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
 *   - ТЕРНАРНИК со строковыми ветками: {cond ? 'Загрузка...' : 'Загрузить файл'}
 *     — берём ОБЕ ветки (рекурсивно, если тернарников несколько подряд).
 *     Работает везде, где раньше ожидался голый литерал: прямой ребёнок
 *     JSX, переводимый атрибут, textContent/innerText, alert()/confirm(),
 *     элементы массива в паттерне .map(), аргументы функции-рендерера.
 *   - МАССИВ/ОБЪЕКТ → .map() → JSX: если элемент массива (или его свойство)
 *     реально используется как видимый текст в колбэке .map(), вытаскиваем
 *     соответствующее значение из КАЖДОГО элемента исходного массива —
 *     например: const tabs = [{label:'Клиенты'}, ...]; tabs.map(tb => <button>{tb.label}</button>)
 *   - ЛОКАЛЬНАЯ ФУНКЦИЯ-РЕНДЕРЕР: если параметр локальной функции уходит
 *     прямо в JSX-текст внутри её же тела, вытаскиваем строковые аргументы
 *     из ВСЕХ вызовов этой функции в файле — например:
 *     const f = (field, label) => <label>{label}</label>;  f('email', 'Email')
 *   - ВЫЗОВ ФУНКЦИИ КАК ВИДИМЫЙ JSX-ТЕКСТ: {subtypeLabel(item)} — резолвим
 *     callee к объявлению функции (локальной в этом же файле — через
 *     Babel scope, либо импортированной из другого файла ЭТОГО ЖЕ пакета
 *     сканирования — через относительный import) и статически обходим её
 *     return-ы, включая return из вложенного вызова другой локальной
 *     функции того же файла (например subtypeLabel() возвращает результат
 *     materialTypeLabel()) — рекурсивно, с защитой от циклов.
 *   - строки, которыми код сам меняет видимый текст страницы:
 *     element.textContent = "...", element.innerText = "...",
 *     а также alert()/confirm() (нативные диалоги — всегда текст для пользователя).
 * Во всех случаях ищем не "любой строковый литерал в файле", а именно
 * строку, для которой АСТ-анализ показывает путь до реального использования
 * в виде JSX-текста — это не слепой сбор всего подряд, а прослеживание
 * конкретной цепочки к экрану. Обычные строковые литералы ВНЕ всех этих
 * путей (пропсы не из белого списка атрибутов, объекты вида styles={...},
 * id, css-значения, служебные строки и т.п.) НЕ трогаем намеренно — иначе
 * ловили бы кучу лишнего шума.
 *
 * Резолвинг импортов (для паттерна "вызов функции") ограничен ОТНОСИТЕЛЬНЫМИ
 * путями (`./`, `../`) внутри пакета файлов, переданных в один вызов этого
 * скрипта — то есть внутри одного проекта. Пакетные импорты (react и т.п.)
 * и `import * as x` намеренно не резолвим — слишком неоднозначно, не тот
 * уровень уверенности, который нужен для автоматического извлечения текста.
 */
const fs = require("fs");
const path = require("path");
const parser = require("@babel/parser");
const traverse = require("@babel/traverse").default;

// Стандартные атрибуты + дополнительные из данных проекта (autoi18n.json
// "attributes", передаются через переменную окружения AUTOI18N_EXTRA_ATTRS).
const TRANSLATABLE_ATTRS = new Set([
  "label", "placeholder", "title", "alt", "aria-label",
  ...String(process.env.AUTOI18N_EXTRA_ATTRS || "").split(",").map((s) => s.trim()).filter(Boolean),
]);
const DISPLAY_PROPS = new Set(["textContent", "innerText", "innerHTML"]);
const DIALOG_FUNCS = new Set(["alert", "confirm"]);
const RESOLVE_EXTENSIONS = [".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"];

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
// разрешённого JSX-атрибута" — используется для .map()-паттерна, функции-
// рендерера и вызова-как-текста, чтобы понять, какая часть реально видна
// на экране как текст.
function isJsxTextSink(exprPath) {
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

// Строковые литералы, статически достижимые из выражения: сам литерал,
// шаблонная строка, либо — рекурсивно — обе ветки тернарника (в любой из
// которых может быть ещё один тернарник). Вызовы функций сюда намеренно
// не подмешиваются — это отдельный, более тяжёлый путь (collectReturnLiterals
// + resolveLocalFunctionPath), используемый только там, где вызов сам
// целиком является JSX-текстом.
function literalsFromExpr(node) {
  if (!node) return [];
  if (node.type === "StringLiteral") {
    return [{ text: node.value, placeholders: 0 }];
  }
  if (node.type === "TemplateLiteral") {
    return [convertTemplateLiteral(node)];
  }
  if (node.type === "ConditionalExpression") {
    return [...literalsFromExpr(node.consequent), ...literalsFromExpr(node.alternate)];
  }
  if (node.type === "LogicalExpression" && (node.operator === "||" || node.operator === "??")) {
    // a || 'Резервный текст' — правая часть видна пользователю, когда a
    // ложно/undefined (частый паттерн запасного значения, например
    // return contentType.split('/')[1]?.toUpperCase() || 'Изображение').
    return literalsFromExpr(node.right);
  }
  return [];
}

function isFunctionNode(node) {
  return (
    node &&
    (node.type === "FunctionDeclaration" ||
      node.type === "FunctionExpression" ||
      node.type === "ArrowFunctionExpression")
  );
}

// Резолвит Identifier к пути локальной функции того же файла через Babel
// scope (объявление function foo() {} или const foo = () => {}/function(){}).
function resolveLocalFunctionPath(atPath, name) {
  const binding = atPath.scope.getBinding(name);
  if (!binding) return null;
  if (binding.path.isFunctionDeclaration()) return binding.path;
  if (binding.path.isVariableDeclarator() && isFunctionNode(binding.path.node.init)) {
    return binding.path.get("init");
  }
  return null;
}

// Статически выводимые return-строки функции: сам литерал/шаблон/тернарник
// в return, а если return вызывает другую ЛОКАЛЬНУЮ функцию того же файла —
// рекурсивно берём и её return-ы тоже (с защитой от циклов через seen).
function collectReturnLiterals(fnPath, seen) {
  seen = seen || new Set();
  if (seen.has(fnPath.node)) return [];
  seen.add(fnPath.node);

  const out = [];

  const handleExpr = (exprPath, exprNode) => {
    if (!exprNode) return;
    out.push(...literalsFromExpr(exprNode));

    const callBranches =
      exprNode.type === "CallExpression"
        ? [exprNode]
        : exprNode.type === "ConditionalExpression"
        ? [exprNode.consequent, exprNode.alternate].filter((n) => n.type === "CallExpression")
        : [];

    for (const call of callBranches) {
      if (call.callee.type !== "Identifier") continue;
      const nested = resolveLocalFunctionPath(exprPath, call.callee.name);
      if (nested) out.push(...collectReturnLiterals(nested, seen));
    }
  };

  const body = fnPath.node.body;
  if (body && body.type !== "BlockStatement") {
    // Стрелочная функция с неявным return: (x) => 'Текст'
    handleExpr(fnPath.get("body"), body);
    return out;
  }

  fnPath.traverse({
    ReturnStatement(rp) {
      const enclosingFn = rp.getFunctionParent();
      if (enclosingFn && enclosingFn.node !== fnPath.node) return; // не залезаем во вложенные функции
      handleExpr(rp.get("argument"), rp.node.argument);
    },
  });
  return out;
}

// {имя_объявления_или_экспорта -> return-литералы} для функций верхнего
// уровня файла — используется и для локальных вызовов в том же файле (как
// "материал" для собственного резолва при первом проходе), и как реестр,
// по которому другие файлы пакета резолвят импортированные вызовы.
function buildFunctionRegistry(ast) {
  const registry = new Map();
  traverse(ast, {
    FunctionDeclaration(p) {
      if (p.node.id) registry.set(p.node.id.name, collectReturnLiterals(p));
    },
    VariableDeclarator(p) {
      if (isFunctionNode(p.node.init) && p.node.id.type === "Identifier") {
        registry.set(p.node.id.name, collectReturnLiterals(p.get("init")));
      }
    },
  });
  return registry;
}

// {локальное_имя -> {source, importedName}} по всем import в файле.
// Резолвим только именованные и default импорты — namespace-импорт
// (import * as x) слишком неоднозначен, не трогаем.
function buildImportMap(ast) {
  const map = new Map();
  traverse(ast, {
    ImportDeclaration(p) {
      const source = p.node.source.value;
      for (const spec of p.node.specifiers) {
        if (spec.type === "ImportSpecifier") {
          map.set(spec.local.name, { source, importedName: spec.imported.name });
        } else if (spec.type === "ImportDefaultSpecifier") {
          map.set(spec.local.name, { source, importedName: "default" });
        }
      }
    },
  });
  return map;
}

function extractFromAst(ast, file, absPath, registries, importMapsByFile, resolveImportPath) {
  const items = [];
  const pushItem = (text, placeholders, line) => {
    if (typeof text !== "string") return;
    const trimmed = text.trim();
    if (!shouldTranslateText(trimmed)) return;
    items.push({ text: trimmed, placeholders, line });
  };
  const pushExpr = (node, line) => {
    for (const { text, placeholders } of literalsFromExpr(node)) {
      pushItem(text, placeholders, line);
    }
  };

  const importMap = importMapsByFile.get(absPath) || new Map();

  // Резолвит вызов функции (как JSX-текст) к её return-литералам: сперва
  // локальная функция того же файла (через scope), иначе — импортированная
  // из локального файла того же пакета сканирования (через реестр,
  // построенный по ВСЕМ файлам пакета заранее).
  function resolveCallReturnLiterals(name, callPath) {
    const localFn = resolveLocalFunctionPath(callPath, name);
    if (localFn) return collectReturnLiterals(localFn);

    const imp = importMap.get(name);
    if (imp) {
      const resolved = resolveImportPath(absPath, imp.source);
      if (resolved) {
        const reg = registries.get(resolved);
        if (reg && reg.has(imp.importedName)) return reg.get(imp.importedName);
      }
    }
    return [];
  }

  // --- Паттерн "массив/объект → .map() → JSX" ---------------------------
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
        for (const idx of slotInfo.values) pushExpr(el.elements[idx], line);
      } else if (slotInfo.kind === "key" && el.type === "ObjectExpression") {
        for (const key of slotInfo.values) {
          const prop = el.properties.find(
            (pr) =>
              pr.type === "ObjectProperty" &&
              !pr.computed &&
              ((pr.key.type === "Identifier" && pr.key.name === key) ||
                (pr.key.type === "StringLiteral" && pr.key.value === key))
          );
          if (prop) pushExpr(prop.value, line);
        }
      }
    }
  }

  // --- Паттерн "локальная функция-рендерер" ------------------------------
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
      } else if (value.type === "JSXExpressionContainer") {
        pushExpr(value.expression, lineOf(p.node));
      }
    },
    JSXExpressionContainer(p) {
      if (p.parent.type !== "JSXElement" && p.parent.type !== "JSXFragment") return;
      pushExpr(p.node.expression, lineOf(p.node));
    },

    AssignmentExpression(p) {
      const left = p.node.left;
      if (
        left.type === "MemberExpression" &&
        left.property.type === "Identifier" &&
        DISPLAY_PROPS.has(left.property.name)
      ) {
        pushExpr(p.node.right, lineOf(p.node));
      }
    },
    CallExpression(p) {
      const callee = p.node.callee;
      const name = callee.type === "Identifier" ? callee.name : null;
      if (name && DIALOG_FUNCS.has(name) && p.node.arguments.length) {
        pushExpr(p.node.arguments[0], lineOf(p.node));
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

      // Паттерн "вызов функции как видимый JSX-текст": {subtypeLabel(item)}
      if (name && isJsxTextSink(p)) {
        for (const { text, placeholders } of resolveCallReturnLiterals(name, p)) {
          pushItem(text, placeholders, lineOf(p.node));
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
          pushExpr(callPath.node.arguments[idx], lineOf(callPath.node));
        }
      }
    },
  });

  return items;
}

function resolveImportPathFactory(parsed) {
  return function resolveImportPath(fromAbsFile, source) {
    if (!source.startsWith(".")) return null; // только локальные файлы проекта
    const base = path.resolve(path.dirname(fromAbsFile), source);
    if (parsed.has(base)) return base;
    for (const ext of RESOLVE_EXTENSIONS) {
      if (parsed.has(base + ext)) return base + ext;
    }
    for (const ext of RESOLVE_EXTENSIONS) {
      const idx = path.join(base, "index" + ext);
      if (parsed.has(idx)) return idx;
    }
    return null;
  };
}

function main() {
  const files = process.argv.slice(2);
  const result = {};
  const errors = {};

  // Один проход: парсим все файлы пакета и строим по каждому реестр
  // функций (нужен и для локальных вызовов, и для резолва импортов между
  // файлами пакета на втором проходе).
  const parsed = new Map(); // absPath -> ast
  const registries = new Map(); // absPath -> Map(name -> literals)
  const importMapsByFile = new Map(); // absPath -> Map(localName -> {source, importedName})
  const absToOriginal = new Map(); // absPath -> исходный аргумент (ключ результата)

  for (const file of files) {
    const absPath = path.resolve(file);
    absToOriginal.set(absPath, file);
    try {
      const code = fs.readFileSync(file, "utf-8");
      const ast = parser.parse(code, {
        sourceType: "unambiguous",
        plugins: ["jsx", "typescript", "classProperties", "decorators-legacy"],
        errorRecovery: true,
      });
      parsed.set(absPath, ast);
    } catch (e) {
      errors[file] = String((e && e.message) || e);
    }
  }

  const resolveImportPath = resolveImportPathFactory(parsed);

  for (const [absPath, ast] of parsed) {
    try {
      registries.set(absPath, buildFunctionRegistry(ast));
      importMapsByFile.set(absPath, buildImportMap(ast));
    } catch (e) {
      const file = absToOriginal.get(absPath);
      errors[file] = String((e && e.message) || e);
      parsed.delete(absPath);
    }
  }

  for (const [absPath, ast] of parsed) {
    const file = absToOriginal.get(absPath);
    try {
      result[file] = extractFromAst(ast, file, absPath, registries, importMapsByFile, resolveImportPath);
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
