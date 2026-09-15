#!/usr/bin/env node
/**
 * autoi18n JS/JSX/TSX extractor — AST-based (Babel).
 *
 * Использование: node extract.js file1.js file2.jsx ...
 * Вывод (stdout): { "<file>": [ {"text","placeholders","line"} ], "_errors": {...} }
 *
 * НИКАКИХ требований к коду проекта — это офлайн-сканирование исходников,
 * ни одна строка кода в самом проекте не нужна ради работы библиотеки.
 * Ищем текст там, где он естественным образом появляется:
 *   - текст и переводимые атрибуты в JSX (видно пользователю по построению);
 *   - строковый литерал или шаблонная строка, вставленные прямо как ДОЧЕРНИЙ
 *     элемент JSX через {...} (например {'Главная'} или {`Вопрос ${n}`});
 *   - МАССИВ/ОБЪЕКТ → .map() → JSX: если элемент массива (или его свойство)
 *     реально используется как видимый текст в колбэке .map(), вытаскиваем
 *     соответствующее значение из КАЖДОГО элемента исходного массива —
 *     например: const tabs = [{label:'Педагоги'}, ...]; tabs.map(tb => <button>{tb.label}</button>)
 *   - ЛОКАЛЬНАЯ ФУНКЦИЯ-РЕНДЕРЕР: если параметр локальной функции уходит
 *     прямо в JSX-текст внутри её же тела, вытаскиваем строковые аргументы
 *     из ВСЕХ вызовов этой функции в файле — например:
 *     const f = (field, label) => <label>{label}</label>;  f('email', 'Email')
 *   - строки, которыми код сам меняет видимый текст страницы:
 *     element.textContent = "...", element.innerText = "...",
 *     а также alert()/confirm() (нативные диалоги — всегда текст для пользователя).
 * Во всех новых случаях (массив→.map(), функция-рендерер) ищем не "любой
 * строковый литерал в файле", а именно строку, для которой АСТ-анализ
 * показывает путь до реального использования в виде JSX-текста — это не
 * слепой сбор всего подряд, а прослеживание конкретной цепочки к экрану.
 * Обычные строковые литералы ВНЕ всех этих путей (пропсы не из белого
 * списка атрибутов, объекты вида styles={...}, id, css-значения, служебные
 * строки и т.п.) НЕ трогаем намеренно — иначе ловили бы кучу лишнего шума.
 */
const fs = require("fs");
const parser = require("@babel/parser");
const traverse = require("@babel/traverse").default;

const TRANSLATABLE_ATTRS = new Set(["label", "placeholder", "title", "aria-label"]);
const DISPLAY_PROPS = new Set(["textContent", "innerText", "innerHTML"]);
const DIALOG_FUNCS = new Set(["alert", "confirm"]);

function shouldTranslateText(text) {
  const t = text.trim();
  if (t.length < 2 || t.length > 200) return false;
  if (!/[A-Za-zА-Яа-яЁё]/.test(t)) return false;
  if (/^[\d\s.,:/\-]+$/.test(t)) return false;
  if (/^#[0-9A-Fa-f]{3,8}$/.test(t)) return false;
  return true;
}

function convertTemplateLiteral(node) {
  let text = "";
  let placeholders = 0;
  node.quasis.forEach((quasi, i) => {
    text += quasi.value.cooked;
    if (i < node.expressions.length) {
      text += `{{${placeholders}}}`;
      placeholders += 1;
    }
  });
  return { text, placeholders };
}

function lineOf(node) {
  return node && node.loc ? node.loc.start.line : null;
}

// Проверка "это выражение — прямой дочерний узел JSX или значение
// разрешённого JSX-атрибута" — используется и для .map()-паттерна, и для
// функции-рендерера, чтобы понять, какая часть параметра реально видна
// на экране как текст.
function isJsxTextSink(exprPath) {
  const parent = exprPath.parentPath;
  if (exprPath.parentPath.isJSXExpressionContainer()) {
    const container = exprPath.parentPath;
    if (container.parentPath.isJSXElement() || container.parentPath.isJSXFragment()) {
      return true; // дочерний элемент JSX
    }
    if (container.parentPath.isJSXAttribute()) {
      const name = container.parentPath.node.name && container.parentPath.node.name.name;
      return TRANSLATABLE_ATTRS.has(name);
    }
  }
  return false;
}

function extractFromFile(filePath) {
  const code = fs.readFileSync(filePath, "utf-8");
  const ast = parser.parse(code, {
    sourceType: "unambiguous",
    plugins: ["jsx", "typescript", "classProperties", "decorators-legacy"],
    errorRecovery: true,
  });

  const items = [];
  const pushItem = (text, placeholders, line) => {
    if (typeof text !== "string") return;
    const trimmed = text.trim();
    if (!shouldTranslateText(trimmed)) return;
    items.push({ text: trimmed, placeholders, line });
  };
  const pushFromNode = (node, line) => {
    if (!node) return;
    if (node.type === "StringLiteral") {
      pushItem(node.value, 0, line);
    } else if (node.type === "TemplateLiteral") {
      const { text, placeholders } = convertTemplateLiteral(node);
      pushItem(text, placeholders, line);
    }
  };

  // --- Паттерн "массив/объект → .map() → JSX" ---------------------------
  // Определяем, какая позиция (для ArrayPattern-параметра) или какое имя
  // свойства (для Identifier- или ObjectPattern-параметра) реально
  // используется в теле колбэка как видимый JSX-текст.
  function findMapTextSlots(callbackPath, paramNode) {
    let paramMode = null;
    let identifierName = null;
    let arrayNames = null;

    if (paramNode.type === "Identifier") {
      paramMode = "identifier";
      identifierName = paramNode.name;
    } else if (paramNode.type === "ArrayPattern") {
      paramMode = "array";
      arrayNames = paramNode.elements.map((el) => (el && el.type === "Identifier" ? el.name : null));
    } else if (paramNode.type === "ObjectPattern") {
      paramMode = "object";
    } else {
      return null;
    }

    const indexSlots = new Set();
    const keySlots = new Set();

    callbackPath.traverse({
      Identifier(p) {
        if (!isJsxTextSink(p)) return;
        if (paramMode === "array") {
          const idx = arrayNames.indexOf(p.node.name);
          if (idx !== -1) indexSlots.add(idx);
        } else if (paramMode === "object") {
          keySlots.add(p.node.name);
        }
      },
      MemberExpression(p) {
        if (paramMode !== "identifier") return;
        if (p.node.computed) return;
        if (p.node.object.type !== "Identifier" || p.node.object.name !== identifierName) return;
        if (p.node.property.type !== "Identifier") return;
        if (!isJsxTextSink(p)) return;
        keySlots.add(p.node.property.name);
      },
    });

    if (indexSlots.size) return { kind: "index", values: indexSlots };
    if (keySlots.size) return { kind: "key", values: keySlots };
    return null;
  }

  function extractFromArrayLiteral(arrNode, slotInfo, line) {
    if (!arrNode || arrNode.type !== "ArrayExpression") return;
    for (const el of arrNode.elements) {
      if (!el) continue;
      if (slotInfo.kind === "index" && el.type === "ArrayExpression") {
        for (const idx of slotInfo.values) {
          const target = el.elements[idx];
          if (target && target.type === "StringLiteral") pushItem(target.value, 0, line);
        }
      } else if (slotInfo.kind === "key" && el.type === "ObjectExpression") {
        for (const key of slotInfo.values) {
          const prop = el.properties.find(
            (pr) =>
              pr.type === "ObjectProperty" &&
              !pr.computed &&
              ((pr.key.type === "Identifier" && pr.key.name === key) ||
                (pr.key.type === "StringLiteral" && pr.key.value === key))
          );
          if (prop && prop.value.type === "StringLiteral") pushItem(prop.value.value, 0, line);
        }
      }
    }
  }

  // --- Паттерн "локальная функция-рендерер" ------------------------------
  // Параметр функции уходит прямо в JSX-текст внутри её собственного тела
  // → вытаскиваем строковые аргументы на той же позиции из ВСЕХ вызовов
  // этой функции в файле (через Babel scope/bindings — не текстовый поиск).
  function findRenderHelperTextParams(fnPath, params) {
    const names = params.map((p) => {
      if (p.type === "Identifier") return p.name;
      if (p.type === "AssignmentPattern" && p.left.type === "Identifier") return p.left.name;
      return null;
    });
    const indices = new Set();

    fnPath.traverse({
      Identifier(p) {
        if (!isJsxTextSink(p)) return;
        const idx = names.indexOf(p.node.name);
        if (idx !== -1) indices.add(idx);
      },
    });

    return indices;
  }

  traverse(ast, {
    // --- JSX: видно пользователю по самой структуре разметки ---
    JSXText(p) {
      pushItem(p.node.value, 0, lineOf(p.node));
    },
    JSXAttribute(p) {
      const name = p.node.name && p.node.name.name;
      if (!TRANSLATABLE_ATTRS.has(name)) return;
      const value = p.node.value;
      if (!value) return;
      if (value.type === "StringLiteral") {
        pushItem(value.value, 0, lineOf(p.node));
      } else if (value.type === "JSXExpressionContainer" && value.expression.type === "StringLiteral") {
        // placeholder={'Название курса'} — тот же литерал, что и
        // placeholder="Название курса", просто в фигурных скобках.
        pushItem(value.expression.value, 0, lineOf(p.node));
      } else if (value.type === "JSXExpressionContainer" && value.expression.type === "TemplateLiteral") {
        const { text, placeholders } = convertTemplateLiteral(value.expression);
        pushItem(text, placeholders, lineOf(p.node));
      }
    },
    // Строка/шаблон, вставленные прямо как дочерний элемент JSX через {...}
    // (например {'Главная'}). Строго позиция "прямой ребёнок
    // <Элемент>...</Элемент>" — у JSX-атрибутов родитель JSXAttribute,
    // эта ветка их не затрагивает и не дублирует JSXAttribute.
    JSXExpressionContainer(p) {
      if (p.parent.type !== "JSXElement" && p.parent.type !== "JSXFragment") return;
      pushFromNode(p.node.expression, lineOf(p.node));
    },

    // --- обычный JS: только места, где строка структурно ЯВЛЯЕТСЯ
    // видимым текстом страницы, без привязки к какой-либо обёртке ---
    AssignmentExpression(p) {
      const left = p.node.left;
      if (
        left.type === "MemberExpression" &&
        left.property.type === "Identifier" &&
        DISPLAY_PROPS.has(left.property.name)
      ) {
        pushFromNode(p.node.right, lineOf(p.node));
      }
    },
    CallExpression(p) {
      const callee = p.node.callee;
      const name = callee.type === "Identifier" ? callee.name : null;
      if (name && DIALOG_FUNCS.has(name) && p.node.arguments.length) {
        pushFromNode(p.node.arguments[0], lineOf(p.node));
      }

      // Паттерн "массив → .map() → JSX"
      if (
        callee.type === "MemberExpression" &&
        !callee.computed &&
        callee.property.type === "Identifier" &&
        callee.property.name === "map" &&
        p.node.arguments.length &&
        (p.node.arguments[0].type === "ArrowFunctionExpression" || p.node.arguments[0].type === "FunctionExpression")
      ) {
        let arrNode = null;
        if (callee.object.type === "ArrayExpression") {
          arrNode = callee.object;
        } else if (callee.object.type === "Identifier") {
          const binding = p.scope.getBinding(callee.object.name);
          if (
            binding &&
            binding.path.isVariableDeclarator() &&
            binding.path.node.id.type === "Identifier" &&
            binding.path.node.init &&
            binding.path.node.init.type === "ArrayExpression"
          ) {
            arrNode = binding.path.node.init;
          }
        }
        if (arrNode) {
          const callback = p.node.arguments[0];
          if (callback.params.length >= 1) {
            const callbackPath = p.get("arguments")[0];
            const slotInfo = findMapTextSlots(callbackPath, callback.params[0]);
            if (slotInfo) extractFromArrayLiteral(arrNode, slotInfo, lineOf(p.node));
          }
        }
      }
    },

    // Паттерн "локальная функция-рендерер"
    VariableDeclarator(p) {
      const init = p.node.init;
      if (!init || (init.type !== "ArrowFunctionExpression" && init.type !== "FunctionExpression")) return;
      if (p.node.id.type !== "Identifier") return;
      const params = init.params;
      if (!params.length) return;

      const fnPath = p.get("init");
      let hasJSX = false;
      fnPath.traverse({
        JSXElement() {
          hasJSX = true;
        },
        JSXFragment() {
          hasJSX = true;
        },
      });
      if (!hasJSX) return;

      const textParamIndices = findRenderHelperTextParams(fnPath, params);
      if (!textParamIndices.size) return;

      const binding = p.scope.getBinding(p.node.id.name);
      if (!binding) return;

      for (const refPath of binding.referencePaths) {
        const callPath = refPath.parentPath;
        if (!callPath || !callPath.isCallExpression() || callPath.node.callee !== refPath.node) continue;
        for (const idx of textParamIndices) {
          const arg = callPath.node.arguments[idx];
          if (!arg) continue;
          pushFromNode(arg, lineOf(callPath.node));
        }
      }
    },
  });

  return items;
}

function main() {
  const files = process.argv.slice(2);
  const result = {};
  const errors = {};

  for (const file of files) {
    try {
      result[file] = extractFromFile(file);
    } catch (e) {
      errors[file] = String((e && e.message) || e);
    }
  }

  if (Object.keys(errors).length) {
    result._errors = errors;
  }
  process.stdout.write(JSON.stringify(result));
}

main();
