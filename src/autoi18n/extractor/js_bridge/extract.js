#!/usr/bin/env node
/**
 * autoi18n JS/JSX/TSX extractor — AST-based (Babel).
 *
 * Использование: node extract.js file1.js file2.jsx ...
 * Вывод (stdout): { "<file>": [ {"text","placeholders","line"} ], "_errors": {...} }
 *
 * НИКАКИХ требований к коду проекта — это офлайн-сканирование исходников,
 * ни одна строка кода в самом проекте не нужна ради работы библиотеки.
 *
 * Принцип один: находим МЕСТА, ГДЕ ТЕКСТ ВЫХОДИТ К ПОЛЬЗОВАТЕЛЮ, и от каждого
 * такого места прослеживаем назад по коду, какие строки могут туда попасть.
 * Собираем только их — не "любой строковый литерал в файле".
 *
 * Места выхода текста:
 *   - текст между тегами JSX и выражение {...} как дочерний элемент JSX;
 *   - значение переводимого атрибута JSX (title, placeholder, alt,
 *     aria-label, label + атрибуты из данных проекта);
 *   - element.textContent / innerText / innerHTML = ...;
 *   - alert(...) / confirm(...), в том числе window.confirm(...),
 *     globalThis.alert(...), self.alert(...).
 *
 * Как прослеживается значение (функция trace):
 *   - строка и шаблонная строка (`Вопрос ${n}` → "Вопрос {{0}}"); склейка
 *     через "+" — та же шаблонная строка: 'Всего: ' + n → "Всего: {{0}}";
 *   - тернарник — обе ветки; a || b, a ?? b — обе части; a && b — правая;
 *   - переменная — её инициализатор и присваивания; деструктуризация
 *     (const { label } = item, const [first] = list) и for (const x of list);
 *   - словарь: LABELS[status], LABELS.done, item.label — в объектном литерале
 *     берётся нужное свойство (при вычисляемом ключе — все), в массиве —
 *     элементы; через spread ({...base}) и Object.values / Object.entries;
 *     плюс то, что в переменную дописали: list.push(x), obj.key = x;
 *   - массив → .map / .filter / .find и т.п.: параметр колбэка — это элемент
 *     массива, на котором метод вызван: tabs.map(tb => <b>{tb.label}</b>);
 *   - вызов функции — её return-ы (локальная функция или импортированная
 *     из файла этого же пакета сканирования); параметр внутри функции
 *     подставляется аргументом именно этого вызова;
 *   - параметр функции — аргументы всех её вызовов и значение по умолчанию;
 *   - проп компонента — значения этого атрибута во всех местах, где
 *     компонент использован в JSX (в том числе в других файлах пакета), и
 *     значение по умолчанию: function Btn({ tip = 'Удалить' }) ...;
 *   - состояние: const [error, setError] = useState('') — начальное значение
 *     и аргументы всех вызовов сеттера. Сеттер прослеживается и там, куда
 *     его передали: <Form onError={setError}/> → вызовы onError('...') внутри
 *     Form; так же для любой функции, переданной пропом или аргументом;
 *   - ошибка в catch (e): e.message — тексты throw new Error('...') из блока
 *     try и из функций, которые в нём вызваны.
 *
 * Чего прослеживание НЕ делает: не заглядывает в ответы сервера и в
 * сторонние пакеты, не собирает части строки, склеенной из кусков
 * (list.join(', '), выражения внутри `${...}`) — на экране это один текст,
 * и по кускам его не сопоставить. Строка, у которой нет
 * прослеживаемого пути к месту выхода текста (css-классы, id, ключи,
 * служебные значения), не собирается.
 *
 * Импорты резолвятся только по ОТНОСИТЕЛЬНЫМ путям (`./`, `../`) внутри
 * пакета файлов, переданных в один вызов этого скрипта — то есть внутри
 * одного проекта. Пакетные импорты (react и т.п.) и `import * as x` не
 * резолвятся.
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
// Глобальные объекты, через которые вызывают те же диалоги: window.confirm(...)
const DIALOG_OBJECTS = new Set(["window", "globalThis", "self"]);
const RESOLVE_EXTENSIONS = [".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"];

// Методы массива, колбэк которых получает элемент массива первым параметром.
const ITERATING_METHODS = new Set([
  "map", "forEach", "filter", "find", "findLast", "some", "every", "flatMap", "findIndex",
]);
// Методы, возвращающие тот же набор элементов (массив из элементов исходного).
const SAME_ITEMS_METHODS = new Set(["filter", "slice", "sort", "reverse", "toSorted", "toReversed", "concat"]);
// Методы, возвращающие один элемент массива.
const ONE_ITEM_METHODS = new Set(["find", "findLast", "at"]);
// Обёртки, возвращающие ту же функцию: useCallback(fn), memo(Component), forwardRef(fn).
const FUNCTION_WRAPPERS = new Set(["useCallback", "memo", "forwardRef"]);

// "Любой ключ": obj[выражение], элемент массива, вычисляемое свойство.
const ANY = "*";
// Предел шагов прослеживания от одного места выхода текста — защита от
// разрастания на запутанном коде (обычно хватает десятков шагов).
const TRACE_BUDGET = 4000;
// Глубина захода в вызванные функции при поиске throw.
const THROW_DEPTH = 3;

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

// Операнды склейки a + b + c слева направо.
function concatOperands(node, out) {
  if (node.type === "BinaryExpression" && node.operator === "+") {
    concatOperands(node.left, out);
    concatOperands(node.right, out);
  } else {
    out.push(node);
  }
  return out;
}

// Склейка строк через "+" как одна фраза с параметрами. null — это не
// склейка строк (нет ни одного строкового куска, например сложение чисел).
function convertConcatenation(node) {
  let text = "";
  let placeholders = 0;
  let hasString = false;
  for (const part of concatOperands(node, [])) {
    if (part.type === "StringLiteral") {
      text += part.value;
      hasString = true;
    } else if (part.type === "TemplateLiteral") {
      part.quasis.forEach((quasi, i) => {
        text += quasi.value.cooked;
        if (i < part.expressions.length) text += `{{${placeholders++}}}`;
      });
      hasString = true;
    } else {
      text += `{{${placeholders++}}}`;
    }
  }
  return hasString ? { text, placeholders } : null;
}

function lineOf(node) {
  return node && node.loc ? node.loc.start.line : null;
}

function isFunctionNode(node) {
  return (
    node &&
    (node.type === "FunctionDeclaration" ||
      node.type === "FunctionExpression" ||
      node.type === "ArrowFunctionExpression")
  );
}

function isCall(pathOrNull) {
  return !!pathOrNull && (pathOrNull.isCallExpression() || pathOrNull.isOptionalCallExpression());
}

function isMember(node) {
  return node.type === "MemberExpression" || node.type === "OptionalMemberExpression";
}

// Имя вызываемого: foo(...) → "foo", React.memo(...) → "memo".
function calleeName(callNode) {
  const callee = callNode.callee;
  if (callee.type === "Identifier") return callee.name;
  if (isMember(callee) && !callee.computed && callee.property.type === "Identifier") return callee.property.name;
  return null;
}

// Имя нативного диалога, если это его вызов: alert(...) или window.alert(...).
function dialogFuncName(callee) {
  let name = null;
  if (callee.type === "Identifier") {
    name = callee.name;
  } else if (
    callee.type === "MemberExpression" &&
    !callee.computed &&
    callee.object.type === "Identifier" &&
    DIALOG_OBJECTS.has(callee.object.name) &&
    callee.property.type === "Identifier"
  ) {
    name = callee.property.name;
  }
  return name && DIALOG_FUNCS.has(name) ? name : null;
}

// Ключ обращения obj.key / obj["key"] / obj[0]; obj[выражение] → ANY.
function memberKey(node) {
  if (!node.computed) return node.property.type === "Identifier" ? node.property.name : null;
  if (node.property.type === "StringLiteral") return node.property.value;
  if (node.property.type === "NumericLiteral") return String(node.property.value);
  return ANY;
}

// Ключ свойства объектного литерала или шаблона деструктуризации.
function propertyKey(prop) {
  if (prop.computed) return ANY;
  if (prop.key.type === "Identifier") return prop.key.name;
  if (prop.key.type === "StringLiteral") return prop.key.value;
  if (prop.key.type === "NumericLiteral") return String(prop.key.value);
  return null;
}

function keysMatch(wanted, actual) {
  return actual !== null && (wanted === ANY || actual === ANY || wanted === actual);
}

// Пути выражений, которые функция возвращает сама (без вложенных функций).
function ownReturnPaths(fnPath) {
  const body = fnPath.node.body;
  if (body && body.type !== "BlockStatement") {
    // Стрелочная функция с неявным return: (x) => 'Текст'
    return [fnPath.get("body")];
  }
  const out = [];
  fnPath.traverse({
    ReturnStatement(rp) {
      const enclosingFn = rp.getFunctionParent();
      if (enclosingFn && enclosingFn.node !== fnPath.node) return; // не залезаем во вложенные функции
      if (rp.node.argument) out.push(rp.get("argument"));
    },
  });
  return out;
}

// Где в шаблоне деструктуризации стоит идентификатор identNode: путь ключей
// от корня шаблона до него и значение по умолчанию, если оно задано прямо у
// него ({ tip = 'Удалить' }). null — идентификатора в шаблоне нет.
function findInPattern(patternPath, identNode) {
  const node = patternPath.node;
  if (!node) return null;
  if (node.type === "Identifier") {
    return node === identNode ? { keys: [], defaultPath: null } : null;
  }
  if (node.type === "AssignmentPattern") {
    const found = findInPattern(patternPath.get("left"), identNode);
    if (found && !found.keys.length && !found.defaultPath) found.defaultPath = patternPath.get("right");
    return found;
  }
  if (node.type === "RestElement") {
    // ...rest — тот же объект/массив (без уже названных ключей)
    return findInPattern(patternPath.get("argument"), identNode);
  }
  if (node.type === "ObjectPattern") {
    for (const prop of patternPath.get("properties")) {
      if (prop.isRestElement()) {
        const found = findInPattern(prop, identNode);
        if (found) return found;
        continue;
      }
      const found = findInPattern(prop.get("value"), identNode);
      if (found) {
        const key = propertyKey(prop.node);
        if (key === null) return null;
        return { keys: [key, ...found.keys], defaultPath: found.defaultPath };
      }
    }
    return null;
  }
  if (node.type === "ArrayPattern") {
    const elements = patternPath.get("elements");
    for (let i = 0; i < elements.length; i++) {
      if (!elements[i].node) continue;
      const found = findInPattern(elements[i], identNode);
      if (found) {
        const key = elements[i].isRestElement() ? ANY : String(i);
        return { keys: [key, ...found.keys], defaultPath: found.defaultPath };
      }
    }
    return null;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Пакет файлов: всё, что нужно для прослеживания между файлами.
// ---------------------------------------------------------------------------
class Package {
  constructor() {
    this.files = new Map(); // absPath -> {absPath, ast, program, exports: Map(name -> NodePath)}
    this.byProgramNode = new Map(); // Program node -> file
    this.importers = new Map(); // "absPath\0exportName" -> [Binding] (привязки в файлах-импортёрах)
    this.exportNamesByNode = new Map(); // node функции/декларатора -> [{file, name}]
  }

  addFile(absPath, ast) {
    let program = null;
    // Полный обход нужен, чтобы Babel построил области видимости и привязки.
    traverse(ast, {
      Program(p) {
        program = p;
      },
    });
    const file = { absPath, ast, program, exports: new Map() };
    this.files.set(absPath, file);
    this.byProgramNode.set(program.node, file);
  }

  fileOf(anyPath) {
    return this.byProgramNode.get(anyPath.scope.getProgramParent().path.node) || null;
  }

  resolveImportPath(fromAbsFile, source) {
    if (!source.startsWith(".")) return null; // только локальные файлы проекта
    const base = path.resolve(path.dirname(fromAbsFile), source);
    if (this.files.has(base)) return base;
    for (const ext of RESOLVE_EXTENSIONS) {
      if (this.files.has(base + ext)) return base + ext;
    }
    for (const ext of RESOLVE_EXTENSIONS) {
      const idx = path.join(base, "index" + ext);
      if (this.files.has(idx)) return idx;
    }
    return null;
  }

  // Второй шаг после addFile для всех файлов: экспорты и обратный индекс импортов.
  link() {
    for (const file of this.files.values()) this.collectExports(file);
    for (const file of this.files.values()) {
      for (const stmt of file.program.get("body")) {
        if (!stmt.isImportDeclaration()) continue;
        const target = this.resolveImportPath(file.absPath, stmt.node.source.value);
        if (!target) continue;
        for (const spec of stmt.node.specifiers) {
          let importedName = null;
          if (spec.type === "ImportDefaultSpecifier") importedName = "default";
          else if (spec.type === "ImportSpecifier") {
            importedName = spec.imported.type === "Identifier" ? spec.imported.name : spec.imported.value;
          }
          if (!importedName) continue; // import * as x — не резолвим
          const binding = file.program.scope.getBinding(spec.local.name);
          if (!binding) continue;
          const key = target + "\0" + importedName;
          if (!this.importers.has(key)) this.importers.set(key, []);
          this.importers.get(key).push(binding);
        }
      }
    }
  }

  collectExports(file) {
    const scope = file.program.scope;
    const setExport = (name, defPath) => {
      if (!defPath || !defPath.node) return;
      file.exports.set(name, defPath);
      const fn = this.functionOf(defPath);
      const node = fn ? fn.node : defPath.node;
      if (!this.exportNamesByNode.has(node)) this.exportNamesByNode.set(node, []);
      this.exportNamesByNode.get(node).push({ file, name });
    };
    const bindingPath = (name) => {
      const binding = scope.getBinding(name);
      return binding ? binding.path : null;
    };

    for (const stmt of file.program.get("body")) {
      if (stmt.isExportNamedDeclaration()) {
        const decl = stmt.get("declaration");
        if (decl.isFunctionDeclaration() && decl.node.id) {
          setExport(decl.node.id.name, decl);
        } else if (decl.isVariableDeclaration()) {
          for (const d of decl.get("declarations")) {
            if (d.node.id.type === "Identifier") setExport(d.node.id.name, d);
          }
        }
        if (!stmt.node.source) {
          for (const spec of stmt.node.specifiers || []) {
            if (spec.type !== "ExportSpecifier" || spec.local.type !== "Identifier") continue;
            const exported = spec.exported.type === "Identifier" ? spec.exported.name : spec.exported.value;
            setExport(exported, bindingPath(spec.local.name));
          }
        }
      } else if (stmt.isExportDefaultDeclaration()) {
        const decl = stmt.get("declaration");
        if (decl.isIdentifier()) setExport("default", bindingPath(decl.node.name));
        else setExport("default", decl);
      }
    }
  }

  // Определение, на которое указывает привязка-импорт: {path} в другом файле.
  resolveImportBinding(binding) {
    const spec = binding.path;
    if (!spec.parentPath || !spec.parentPath.isImportDeclaration()) return null;
    let importedName = null;
    if (spec.isImportDefaultSpecifier()) importedName = "default";
    else if (spec.isImportSpecifier()) {
      const imported = spec.node.imported;
      importedName = imported.type === "Identifier" ? imported.name : imported.value;
    }
    if (!importedName) return null;
    const from = this.fileOf(spec);
    if (!from) return null;
    const target = this.resolveImportPath(from.absPath, spec.parentPath.node.source.value);
    if (!target) return null;
    return this.files.get(target).exports.get(importedName) || null;
  }

  // Определение значения по привязке: для импорта — определение в другом
  // файле, иначе — путь самой привязки.
  definitionOf(binding) {
    if (binding.kind === "module") return this.resolveImportBinding(binding);
    return binding.path;
  }

  // Функция, которой является выражение/объявление: сама функция,
  // переменная с функцией, обёртка memo(fn)/useCallback(fn), имя функции.
  functionOf(defPath, depth = 0) {
    if (!defPath || !defPath.node || depth > 8) return null;
    if (isFunctionNode(defPath.node)) return defPath;
    if (defPath.isVariableDeclarator()) {
      if (defPath.node.id.type !== "Identifier") return null;
      return this.functionOf(defPath.get("init"), depth + 1);
    }
    if (isCall(defPath) && FUNCTION_WRAPPERS.has(calleeName(defPath.node))) {
      return this.functionOf(defPath.get("arguments")[0], depth + 1);
    }
    if (defPath.isIdentifier()) {
      const binding = defPath.scope.getBinding(defPath.node.name);
      if (!binding || binding.kind === "param") return null;
      return this.functionOf(this.definitionOf(binding), depth + 1);
    }
    return null;
  }

  // Привязки, под именами которых функция известна: её собственное имя в
  // файле и имена, под которыми её импортируют другие файлы пакета.
  bindingsOfFunction(fnPath) {
    const bindings = [];
    // Имя в своём файле: function f() {}, const f = fn, const f = memo(fn).
    let holder = fnPath;
    while (isCall(holder.parentPath) && FUNCTION_WRAPPERS.has(calleeName(holder.parentPath.node))) {
      holder = holder.parentPath;
    }
    if (fnPath.isFunctionDeclaration() && fnPath.node.id) {
      const b = fnPath.parentPath.scope.getBinding(fnPath.node.id.name);
      if (b) bindings.push(b);
    } else if (holder.parentPath.isVariableDeclarator() && holder.parentPath.node.id.type === "Identifier") {
      const b = holder.parentPath.scope.getBinding(holder.parentPath.node.id.name);
      if (b) bindings.push(b);
    }
    for (const { file, name } of this.exportNamesByNode.get(fnPath.node) || []) {
      bindings.push(...(this.importers.get(file.absPath + "\0" + name) || []));
    }
    return bindings;
  }

  // Места в теле функции, где читается её параметр номер index (key === null)
  // или его свойство key: ({ key }) / props.key / const { key } = props.
  paramReads(fnPath, index, key) {
    const param = fnPath.get("params")[index];
    if (!param || !param.node) return [];
    const reads = [];
    const target = param.isAssignmentPattern() ? param.get("left") : param;

    if (target.isIdentifier()) {
      const binding = fnPath.scope.getBinding(target.node.name);
      if (!binding) return [];
      if (key === null) return binding.referencePaths.slice();
      for (const ref of binding.referencePaths) {
        const parent = ref.parentPath;
        if (isMember(parent.node) && parent.node.object === ref.node && keysMatch(key, memberKey(parent.node))) {
          reads.push(parent);
        } else if (parent.isVariableDeclarator() && parent.node.init === ref.node && parent.get("id").isObjectPattern()) {
          reads.push(...this.patternKeyReads(parent.get("id"), key, parent.scope));
        }
      }
      return reads;
    }
    if (target.isObjectPattern() && key !== null) {
      return this.patternKeyReads(target, key, fnPath.scope);
    }
    return [];
  }

  // Чтения локальной переменной, в которую шаблон { key } кладёт свойство key.
  patternKeyReads(patternPath, key, scope) {
    const reads = [];
    for (const prop of patternPath.get("properties")) {
      if (!prop.isObjectProperty() || propertyKey(prop.node) !== key) continue;
      let value = prop.get("value");
      if (value.isAssignmentPattern()) value = value.get("left");
      if (!value.isIdentifier()) continue;
      const binding = scope.getBinding(value.node.name);
      if (binding) reads.push(...binding.referencePaths);
    }
    return reads;
  }

  // Компонент, на который ссылается открывающий тег <Name ...>.
  componentOf(openingPath) {
    const name = openingPath.node.name;
    if (name.type !== "JSXIdentifier") return null;
    const binding = openingPath.scope.getBinding(name.name);
    if (!binding || binding.kind === "param") return null;
    return this.functionOf(this.definitionOf(binding));
  }

  // Места использования значения-функции, стоящего в выражении exprPath:
  // где её вызывают ({kind:"call"}), где она — колбэк перебора массива
  // ({kind:"iter"}). Значение прослеживается вперёд: через переменную,
  // проп компонента, аргумент другой функции.
  functionUses(exprPath, out, seen) {
    if (seen.has(exprPath.node)) return;
    seen.add(exprPath.node);
    const parent = exprPath.parentPath;
    if (!parent) return;

    if (isCall(parent) && parent.node.callee === exprPath.node) {
      out.push({ kind: "call", call: parent });
      return;
    }
    if (isCall(parent) && exprPath.listKey === "arguments") {
      const index = exprPath.key;
      const name = calleeName(parent.node);
      if (isMember(parent.node.callee)) {
        if (index === 0 && ITERATING_METHODS.has(name)) {
          out.push({ kind: "iter", receiver: parent.get("callee").get("object") });
        }
        if (!FUNCTION_WRAPPERS.has(name)) return;
      }
      if (FUNCTION_WRAPPERS.has(name)) {
        if (index === 0) this.functionUses(parent, out, seen);
        return;
      }
      const callee = this.functionOf(parent.get("callee"));
      if (callee) {
        for (const read of this.paramReads(callee, index, null)) this.functionUses(read, out, seen);
      }
      return;
    }
    if (parent.isJSXExpressionContainer() && parent.parentPath.isJSXAttribute()) {
      const attr = parent.parentPath;
      const component = this.componentOf(attr.parentPath);
      if (component && attr.node.name.type === "JSXIdentifier") {
        for (const read of this.paramReads(component, 0, attr.node.name.name)) this.functionUses(read, out, seen);
      }
      return;
    }
    if (parent.isVariableDeclarator() && parent.node.init === exprPath.node && parent.node.id.type === "Identifier") {
      const binding = parent.scope.getBinding(parent.node.id.name);
      if (binding) for (const ref of binding.referencePaths) this.functionUses(ref, out, seen);
      return;
    }
    if (
      (parent.isConditionalExpression() && parent.node.test !== exprPath.node) ||
      parent.isLogicalExpression() ||
      parent.isParenthesizedExpression()
    ) {
      this.functionUses(parent, out, seen);
    }
  }

  // Все места, откуда функция получает аргументы: вызовы, использование
  // как компонента в JSX ({kind:"jsx"}), перебор массива.
  usesOfFunction(fnPath) {
    const out = [];
    const seen = new Set();
    const bindings = this.bindingsOfFunction(fnPath);
    if (!bindings.length) {
      // Безымянная функция: (x) => ... прямо в аргументе или в пропе.
      let holder = fnPath;
      while (isCall(holder.parentPath) && FUNCTION_WRAPPERS.has(calleeName(holder.parentPath.node))) {
        holder = holder.parentPath;
      }
      this.functionUses(holder, out, seen);
      return out;
    }
    for (const binding of bindings) {
      for (const ref of binding.referencePaths) {
        if (ref.isJSXIdentifier()) {
          if (ref.parentPath.isJSXOpeningElement()) out.push({ kind: "jsx", element: ref.parentPath });
        } else {
          this.functionUses(ref, out, seen);
        }
      }
    }
    return out;
  }
}

// ---------------------------------------------------------------------------
// Прослеживание значения назад от места выхода текста.
// ---------------------------------------------------------------------------
class Tracer {
  constructor(pkg, emit) {
    this.pkg = pkg;
    this.emit = emit; // (text, placeholders) => void
    this.budget = TRACE_BUDGET;
    this.seen = new Map(); // node -> Set("ключи|контекст")
  }

  // want — какие ключи ещё предстоит взять у значения: [] — текст само
  // значение; ["label"] — текст лежит в его свойстве label; ANY — любой ключ.
  // stack — вызовы, внутрь которых зашло прослеживание: [{fn, call}].
  trace(p, want, stack) {
    if (!p || !p.node) return;
    if (this.budget <= 0) return;
    this.budget -= 1;

    const node = p.node;
    const top = stack.length ? stack[stack.length - 1].call.node : null;
    const mark = want.join("\u0001") + "|" + (top ? top.start : "");
    let marks = this.seen.get(node);
    if (!marks) this.seen.set(node, (marks = new Set()));
    if (marks.has(mark)) return;
    marks.add(mark);

    switch (node.type) {
      case "StringLiteral":
        if (!want.length) this.emit(node.value, 0);
        return;
      case "TemplateLiteral":
        if (!want.length) {
          const { text, placeholders } = convertTemplateLiteral(node);
          this.emit(text, placeholders);
        }
        return;
      case "BinaryExpression":
        if (!want.length && node.operator === "+") {
          const phrase = convertConcatenation(node);
          if (phrase) this.emit(phrase.text, phrase.placeholders);
        }
        return;
      case "ConditionalExpression":
        this.trace(p.get("consequent"), want, stack);
        this.trace(p.get("alternate"), want, stack);
        return;
      case "LogicalExpression":
        if (node.operator !== "&&") this.trace(p.get("left"), want, stack);
        this.trace(p.get("right"), want, stack);
        return;
      case "ParenthesizedExpression":
      case "TSAsExpression":
      case "TSSatisfiesExpression":
      case "TSNonNullExpression":
      case "TypeCastExpression":
        this.trace(p.get("expression"), want, stack);
        return;
      case "AwaitExpression":
        this.trace(p.get("argument"), want, stack);
        return;
      case "AssignmentExpression":
        if (node.operator === "=") this.trace(p.get("right"), want, stack);
        return;
      case "SequenceExpression": {
        const expressions = p.get("expressions");
        this.trace(expressions[expressions.length - 1], want, stack);
        return;
      }
      case "ObjectExpression":
        this.traceObject(p, want, stack);
        return;
      case "ArrayExpression":
        this.traceArray(p, want, stack);
        return;
      case "MemberExpression":
      case "OptionalMemberExpression": {
        const key = memberKey(node);
        if (key !== null) this.trace(p.get("object"), [key, ...want], stack);
        return;
      }
      case "Identifier":
        this.traceIdentifier(p, want, stack);
        return;
      case "CallExpression":
      case "OptionalCallExpression":
        this.traceCall(p, want, stack);
        return;
      case "NewExpression":
        // new Error('текст').message
        if (
          want[0] === "message" &&
          node.callee.type === "Identifier" &&
          /Error$/.test(node.callee.name)
        ) {
          this.trace(p.get("arguments")[0], want.slice(1), stack);
        }
        return;
      default:
        return;
    }
  }

  traceObject(p, want, stack) {
    if (!want.length) return;
    const [key, ...rest] = want;
    for (const prop of p.get("properties")) {
      if (prop.isSpreadElement()) {
        this.trace(prop.get("argument"), want, stack);
      } else if (prop.isObjectProperty() && keysMatch(key, propertyKey(prop.node))) {
        this.trace(prop.get("value"), rest, stack);
      }
    }
  }

  traceArray(p, want, stack) {
    if (!want.length) return;
    const [key, ...rest] = want;
    if (key !== ANY && !/^\d+$/.test(key)) return; // .length и т.п.
    const elements = p.get("elements");
    const hasSpread = elements.some((el) => el.node && el.isSpreadElement());
    elements.forEach((el, i) => {
      if (!el.node) return;
      if (el.isSpreadElement()) {
        this.trace(el.get("argument"), want, stack);
      } else if (key === ANY || hasSpread || String(i) === key) {
        this.trace(el, rest, stack);
      }
    });
  }

  traceIdentifier(p, want, stack) {
    const binding = p.scope.getBinding(p.node.name);
    if (!binding) return;

    if (binding.kind === "param") {
      this.traceParam(binding, want, stack);
      return;
    }
    if (binding.kind === "module") {
      const def = this.pkg.resolveImportBinding(binding);
      if (def && def.isVariableDeclarator()) this.trace(def.get("init"), want, []);
      return;
    }
    if (binding.path.isCatchClause()) {
      this.traceCaught(binding.path, want, stack);
      return;
    }
    if (binding.path.isVariableDeclarator()) {
      this.traceDeclared(binding, want, stack);
      this.traceAdditions(binding, want, stack);
      for (const violation of binding.constantViolations) {
        if (
          violation.isAssignmentExpression() &&
          violation.node.operator === "=" &&
          violation.node.left.type === "Identifier"
        ) {
          this.trace(violation.get("right"), want, stack);
        }
      }
    }
  }

  // То, что в переменную дописали после объявления: list.push(x), obj.key = x.
  traceAdditions(binding, want, stack) {
    if (!want.length) return;
    const [key, ...rest] = want;
    const isIndex = key === ANY || /^\d+$/.test(key);
    for (const ref of binding.referencePaths) {
      const member = ref.parentPath;
      if (!isMember(member.node) || member.node.object !== ref.node) continue;
      const memberName = memberKey(member.node);
      const outer = member.parentPath;
      if (isCall(outer) && outer.node.callee === member.node) {
        if (!isIndex || (memberName !== "push" && memberName !== "unshift")) continue;
        for (const arg of outer.get("arguments")) {
          if (arg.isSpreadElement()) this.trace(arg.get("argument"), want, stack);
          else this.trace(arg, rest, stack);
        }
      } else if (
        outer.isAssignmentExpression() &&
        outer.node.left === member.node &&
        outer.node.operator === "=" &&
        keysMatch(key, memberName)
      ) {
        this.trace(outer.get("right"), rest, stack);
      }
    }
  }

  // Переменная: const x = ..., const { a } = ..., const [v, setV] = useState(...),
  // for (const x of ...).
  traceDeclared(binding, want, stack) {
    const decl = binding.path;
    const found = findInPattern(decl.get("id"), binding.identifier);
    if (!found) return;
    if (found.defaultPath) this.trace(found.defaultPath, want, stack);
    const full = [...found.keys, ...want];

    const init = decl.get("init");
    if (!init.node) {
      const loop = decl.parentPath.parentPath;
      if (loop && loop.isForOfStatement() && loop.node.left === decl.parent) {
        this.trace(loop.get("right"), [ANY, ...full], stack);
      }
      return;
    }
    if (decl.get("id").isArrayPattern() && isCall(init) && calleeName(init.node) === "useState") {
      if (found.keys[0] === "0") this.traceState(decl, full.slice(1), stack);
      return;
    }
    this.trace(init, full, stack);
  }

  // Состояние: начальное значение и всё, что передают в сеттер — где бы его
  // ни вызвали (в том числе переданный пропом или аргументом).
  traceState(decl, want, stack) {
    const written = (valuePath) => {
      if (!valuePath || !valuePath.node) return;
      if (isFunctionNode(valuePath.node)) {
        // useState(() => начальное) / setX(prev => следующее)
        for (const ret of ownReturnPaths(valuePath)) this.trace(ret, want, []);
      } else {
        this.trace(valuePath, want, valuePath.scope === decl.scope ? stack : []);
      }
    };
    written(decl.get("init").get("arguments")[0]);

    const setter = decl.node.id.elements[1];
    if (!setter || setter.type !== "Identifier") return;
    const binding = decl.scope.getBinding(setter.name);
    if (!binding) return;
    const uses = [];
    const seen = new Set();
    for (const ref of binding.referencePaths) this.pkg.functionUses(ref, uses, seen);
    for (const use of uses) {
      if (use.kind === "call") written(use.call.get("arguments")[0]);
    }
  }

  // Параметр функции (в том числе проп компонента и параметр колбэка).
  traceParam(binding, want, stack) {
    const fnPath = binding.scope.path;
    if (!isFunctionNode(fnPath.node)) return;
    const params = fnPath.get("params");
    let index = -1;
    let found = null;
    for (let i = 0; i < params.length && !found; i++) {
      found = findInPattern(params[i], binding.identifier);
      if (found) index = i;
    }
    if (!found) return;
    if (params[index].isRestElement()) return; // (...args) — позиция аргумента неизвестна
    if (found.defaultPath) this.trace(found.defaultPath, want, stack);
    const full = [...found.keys, ...want];

    // Прослеживание зашло в эту функцию из конкретного вызова — берём
    // аргумент именно этого вызова, а не всех вызовов функции.
    for (let i = stack.length - 1; i >= 0; i--) {
      if (stack[i].fn === fnPath.node) {
        this.trace(stack[i].call.get("arguments")[index], full, stack.slice(0, i));
        return;
      }
    }

    for (const use of this.pkg.usesOfFunction(fnPath)) {
      if (use.kind === "call") {
        const arg = use.call.get("arguments")[index];
        if (arg && !arg.isSpreadElement()) this.trace(arg, full, []);
      } else if (use.kind === "iter") {
        if (index === 0) this.trace(use.receiver, [ANY, ...full], []);
      } else if (use.kind === "jsx" && index === 0) {
        this.traceJsxProp(use.element, full);
      }
    }
  }

  // Значение пропа want[0] в конкретном использовании компонента <C prop=... />.
  traceJsxProp(openingPath, want) {
    if (!want.length) return;
    const [key, ...rest] = want;
    for (const attr of openingPath.get("attributes")) {
      if (attr.isJSXSpreadAttribute()) {
        this.trace(attr.get("argument"), want, []);
        continue;
      }
      if (attr.node.name.type !== "JSXIdentifier" || !keysMatch(key, attr.node.name.name)) continue;
      const value = attr.get("value");
      if (!value.node) continue;
      if (value.isStringLiteral()) {
        if (!rest.length) this.emit(value.node.value, 0);
      } else if (value.isJSXExpressionContainer()) {
        this.trace(value.get("expression"), rest, []);
      }
    }
  }

  traceCall(p, want, stack) {
    const node = p.node;
    const callee = p.get("callee");
    const name = calleeName(node);
    const args = p.get("arguments");

    if (name === "useMemo" && args[0] && isFunctionNode(args[0].node)) {
      for (const ret of ownReturnPaths(args[0])) this.trace(ret, want, stack);
      return;
    }

    if (isMember(node.callee)) {
      const object = callee.get("object");
      if (object.isIdentifier({ name: "Object" }) && !object.scope.getBinding("Object")) {
        // Object.values(D)[i] — это D[любой ключ]; Object.entries(D)[i][1] — тоже.
        if (name === "values" && want.length) this.trace(args[0], want, stack);
        if (name === "entries" && want.length >= 2 && keysMatch(want[1], "1")) {
          this.trace(args[0], [ANY, ...want.slice(2)], stack);
        }
        return;
      }
      if (SAME_ITEMS_METHODS.has(name)) {
        this.trace(object, want, stack);
        if (name === "concat") for (const arg of args) this.trace(arg, want, stack);
        return;
      }
      if (ONE_ITEM_METHODS.has(name)) {
        this.trace(object, [ANY, ...want], stack);
        return;
      }
      if (name === "map" && want.length && args[0] && isFunctionNode(args[0].node)) {
        // items.map(i => i.label)[n] — return-ы колбэка
        for (const ret of ownReturnPaths(args[0])) this.trace(ret, want.slice(1), stack);
      }
      return;
    }

    if (node.callee.type !== "Identifier") return;
    if (name === "String" && !p.scope.getBinding("String")) {
      this.trace(args[0], want, stack);
      return;
    }
    const fn = this.pkg.functionOf(callee);
    if (!fn) return;
    const inner = [...stack, { fn: fn.node, call: p }];
    for (const ret of ownReturnPaths(fn)) this.trace(ret, want, inner);
  }

  // catch (e): значения, брошенные в блоке try и в функциях, вызванных в нём.
  traceCaught(catchPath, want, stack) {
    const tryPath = catchPath.parentPath;
    if (!tryPath.isTryStatement()) return;
    this.traceThrows(tryPath.get("block"), want, stack, THROW_DEPTH, new Set());
  }

  traceThrows(bodyPath, want, stack, depth, seenFns) {
    const self = this;
    bodyPath.traverse({
      ThrowStatement(tp) {
        self.trace(tp.get("argument"), want, stack);
      },
      "CallExpression|OptionalCallExpression"(cp) {
        if (depth <= 0 || cp.node.callee.type !== "Identifier") return;
        const fn = self.pkg.functionOf(cp.get("callee"));
        if (!fn || seenFns.has(fn.node)) return;
        seenFns.add(fn.node);
        self.traceThrows(fn.get("body"), want, [...stack, { fn: fn.node, call: cp }], depth - 1, seenFns);
      },
    });
  }
}

// ---------------------------------------------------------------------------
// Места выхода текста в одном файле.
// ---------------------------------------------------------------------------
function extractFromFile(pkg, file) {
  const items = [];
  const pushItem = (text, placeholders, line) => {
    if (typeof text !== "string") return;
    const trimmed = text.trim();
    if (!shouldTranslateText(trimmed)) return;
    items.push({ text: trimmed, placeholders, line });
  };
  // Выражение, значение которого выходит к пользователю как текст.
  const pushShown = (exprPath, line) => {
    const tracer = new Tracer(pkg, (text, placeholders) => pushItem(text, placeholders, line));
    tracer.trace(exprPath, [], []);
  };

  traverse(file.ast, {
    JSXText(p) {
      pushItem(p.node.value, 0, lineOf(p.node));
    },
    JSXAttribute(p) {
      const name = p.node.name && p.node.name.name;
      if (!TRANSLATABLE_ATTRS.has(name)) return;
      const value = p.get("value");
      if (!value.node) return;
      if (value.isStringLiteral()) {
        pushItem(value.node.value, 0, lineOf(p.node));
      } else if (value.isJSXExpressionContainer()) {
        pushShown(value.get("expression"), lineOf(p.node));
      }
    },
    JSXExpressionContainer(p) {
      if (p.parent.type !== "JSXElement" && p.parent.type !== "JSXFragment") return;
      pushShown(p.get("expression"), lineOf(p.node));
    },
    AssignmentExpression(p) {
      const left = p.node.left;
      if (
        left.type === "MemberExpression" &&
        left.property.type === "Identifier" &&
        DISPLAY_PROPS.has(left.property.name)
      ) {
        pushShown(p.get("right"), lineOf(p.node));
      }
    },
    CallExpression(p) {
      if (dialogFuncName(p.node.callee) && p.node.arguments.length) {
        pushShown(p.get("arguments")[0], lineOf(p.node));
      }
    },
  });

  return items;
}

function main() {
  const files = process.argv.slice(2);
  const result = {};
  const errors = {};

  // Сначала разбираем все файлы пакета и связываем их между собой (экспорты,
  // импорты) — прослеживание значения может уходить в другие файлы.
  const pkg = new Package();
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
      pkg.addFile(absPath, ast);
    } catch (e) {
      errors[file] = String((e && e.message) || e);
    }
  }

  try {
    pkg.link();
  } catch (e) {
    for (const absPath of pkg.files.keys()) errors[absToOriginal.get(absPath)] = String((e && e.message) || e);
    pkg.files.clear();
  }

  for (const [absPath, fileInfo] of pkg.files) {
    const file = absToOriginal.get(absPath);
    try {
      result[file] = extractFromFile(pkg, fileInfo);
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
