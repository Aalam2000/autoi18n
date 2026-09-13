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
 *   - строки, которыми код сам меняет видимый текст страницы:
 *     element.textContent = "...", element.innerText = "...",
 *     а также alert()/confirm() (нативные диалоги — всегда текст для пользователя).
 * Обычные строковые литералы вне этих мест НЕ трогаем намеренно — иначе
 * задевали бы css-значения, id, служебные строки и т.п. (объекты styles={...}
 * в JSX-компонентах, например).
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
      } else if (value.type === "JSXExpressionContainer" && value.expression.type === "TemplateLiteral") {
        const { text, placeholders } = convertTemplateLiteral(value.expression);
        pushItem(text, placeholders, lineOf(p.node));
      }
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
