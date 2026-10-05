"""2.2.6: извлечение текста прослеживанием значения от места, где оно выходит на экран."""
import shutil
import subprocess

import pytest

from autoi18n.extractor import js_extractor
from autoi18n.runtime import build_frontend_runtime_script


def _bridge_ready():
    node = shutil.which("node")
    if not node:
        return False
    check = subprocess.run([node, "-e", "require('@babel/parser'); require('@babel/traverse')"],
                           cwd=str(js_extractor._BRIDGE_DIR), capture_output=True)
    return check.returncode == 0


BRIDGE_READY = _bridge_ready()


def texts(tmp_path, files, attrs=None):
    """files: {имя: код} -> множество фраз, извлечённых из всех файлов пакета."""
    if not BRIDGE_READY:
        pytest.skip("нужен Node.js и зависимости js_bridge (npm install)")
    paths = []
    for name, code in files.items():
        path = tmp_path / name
        path.write_text(code, encoding="utf-8")
        paths.append(str(path))
    result = js_extractor.extract_js_items_from_files(paths, attrs)
    assert not js_extractor.LAST_PROBLEMS["files"]
    return {item["text"] for items in result.values() for item in items}


def test_dialogs_via_global_object(tmp_path):
    got = texts(tmp_path, {"a.jsx": """
        function f(name, ok) {
          if (!window.confirm(`Удалить ${name}?`)) return;
          window.alert(ok ? 'Готово' : 'Не вышло');
          globalThis.alert('Третий');
          logger.alert('Служебное');
          window.open('Не диалог');
        }
    """})
    assert got == {"Удалить {{0}}?", "Готово", "Не вышло", "Третий"}


def test_state_shown_in_jsx(tmp_path):
    got = texts(tmp_path, {"a.jsx": """
        import { useState } from 'react';
        function Form() {
          const [error, setError] = useState('Начальный текст');
          const [mode, setMode] = useState('Режим');
          const save = async () => {
            setError('');
            if (!mode) { setError('Заполните поле'); return; }
            try { await go(); setMode('Служебное'); }
            catch (e) { setError(e.message || `Ошибка ${e.code}`); }
            setError(mode ? 'Первая ветка' : 'Вторая ветка');
          };
          return <div>{error && <p>{error}</p>}</div>;
        }
    """})
    assert got == {"Начальный текст", "Заполните поле", "Ошибка {{0}}", "Первая ветка", "Вторая ветка"}


def test_state_in_attribute_and_react_namespace(tmp_path):
    got = texts(tmp_path, {"a.jsx": """
        function Field() {
          const [hint, setHint] = React.useState('');
          return <input title={hint} onFocus={() => setHint('Подсказка')} />;
        }
    """})
    assert got == {"Подсказка"}


def test_state_object_field(tmp_path):
    got = texts(tmp_path, {"a.jsx": """
        function Profile() {
          const [msg, setMsg] = useState(null);
          const ok = () => setMsg({ type: 'успех', text: 'Сохранено!' });
          const bad = (e) => setMsg(e ? { type: 'ошибка', text: e.detail || 'Ошибка' } : null);
          return <p className={msg?.type}>{msg?.text}</p>;
        }
    """})
    assert got == {"Сохранено!", "Ошибка"}


def test_state_from_function_result(tmp_path):
    got = texts(tmp_path, {
        "a.jsx": """
            import { errorText, plain } from './errors';
            const local = (e, fallback = 'По умолчанию') => e?.detail ?? fallback;
            function Page() {
              const [error, setError] = useState('');
              const load = () => api().catch(e => setError(errorText(e, 'Не удалось загрузить')));
              const save = () => api().catch(e => setError(local(e, 'Не удалось сохранить')));
              const drop = () => api().catch(e => setError(plain(e, 'Не текст')));
              return <p>{error}</p>;
            }
        """,
        "errors.js": """
            export function errorText(err, fallback) {
              if (err.status === 409) return 'Уже существует';
              const detail = err.detail;
              if (typeof detail === 'string') return detail;
              return fallback;
            }
            export function plain(err, tag) { log(tag); return err.detail; }
        """,
    })
    assert got == {"Не удалось загрузить", "Уже существует", "Не удалось сохранить", "По умолчанию"}


def test_state_from_wrapper_parameter(tmp_path):
    got = texts(tmp_path, {"a.jsx": """
        function Files() {
          const [error, setError] = useState('');
          const run = async (fn, fallback) => {
            try { await fn(); } catch (e) { setError(e.detail || fallback); }
          };
          function fail(text = 'Запасной текст') { setError(text); }
          return <div>
            <button onClick={() => run(remove, 'Не удалось удалить')} />
            <button onClick={() => run(upload, busy ? 'Занято' : 'Не удалось загрузить')} />
            <button onClick={() => fail('Отказ')} />
            <p>{error}</p>
          </div>;
        }
    """})
    assert got == {"Не удалось удалить", "Занято", "Не удалось загрузить", "Запасной текст", "Отказ"}


def test_state_not_shown_is_ignored(tmp_path):
    got = texts(tmp_path, {"a.jsx": """
        function Tabs() {
          const [tab, setTab] = useState('Вкладка');
          const [sort, setSort] = useState('Имя');
          useEffect(() => { setTab('Другая'); setSort('Дата'); }, []);
          return <List active={tab} sort={sort} className={tab} />;
        }
    """})
    assert got == set()


# --- то, что собиралось и раньше -------------------------------------------

def test_plain_jsx_and_attributes(tmp_path):
    got = texts(tmp_path, {"a.jsx": """
        const Page = ({ n, busy, name }) => <div className="Класс" id="Идентификатор">
          Простой текст
          <input placeholder="Поиск" title={busy ? 'Занято' : 'Свободно'} data-x="Не атрибут" />
          {'Главная'} {`Вопрос ${n}`} {name || 'Без имени'} {busy && 'Подождите'}
        </div>;
    """})
    assert got == {"Простой текст", "Поиск", "Занято", "Свободно", "Главная", "Вопрос {{0}}", "Без имени", "Подождите"}


def test_array_map(tmp_path):
    got = texts(tmp_path, {"a.jsx": """
        const tabs = [{ id: 'служебный', label: 'Клиенты' }, { id: 'b', label: 'Заказы' }];
        const pairs = [['k1', 'Первый'], ['k2', 'Второй']];
        const days = ['Пн', 'Вт'];
        const Page = () => <div>
          {tabs.map(tb => <button key={tb.id}>{tb.label}</button>)}
          {pairs.map(([key, title]) => <i key={key}>{title}</i>)}
          {days.filter(Boolean).map((d, i) => <b key={i}>{d}</b>)}
          {[{ text: 'Встроенный' }].map(({ text }) => <u>{text}</u>)}
        </div>;
    """})
    assert got == {"Клиенты", "Заказы", "Первый", "Второй", "Пн", "Вт", "Встроенный"}


def test_render_helper_and_call_as_text(tmp_path):
    got = texts(tmp_path, {
        "a.jsx": """
            import { typeLabel } from './labels';
            function local(item) { return item.ok ? 'Да' : other(item); }
            function other(item) { return item.x || 'Нет'; }
            const Page = ({ item }) => {
              const field = (name, label) => <label htmlFor={name}>{label}</label>;
              return <div>{field('email', 'Почта')}{field('служебное', 'Телефон')}{local(item)}{typeLabel(item)}</div>;
            };
        """,
        "labels.js": """
            export function typeLabel(item) {
              if (item.kind === 'служебное') return 'Файл';
              return item.kind === 'q' ? 'Квиз' : 'Ссылка';
            }
        """,
    })
    assert got == {"Почта", "Телефон", "Да", "Нет", "Файл", "Квиз", "Ссылка"}


def test_text_content_and_dialogs(tmp_path):
    got = texts(tmp_path, {"a.js": """
        el.textContent = 'Счёт';
        el.innerHTML = ok ? 'Верно' : 'Неверно';
        el.className = 'Класс';
        alert('Сохранено');
        if (confirm(`Удалить ${n}?`)) go('Служебное');
    """})
    assert got == {"Счёт", "Верно", "Неверно", "Сохранено", "Удалить {{0}}?"}


# --- словари, переменные, склейка ------------------------------------------

def test_dictionary_by_key(tmp_path):
    got = texts(tmp_path, {
        "a.jsx": """
            import { META } from './meta';
            const STATUS = { done: 'Готово', wait: 'Ждёт', cls: { done: 'Зелёный' } };
            const MONTHS = ['Январь', 'Февраль'];
            const FLAGS = { none: { cls: 'серый', tip: 'Нет' }, ok: { cls: 'зелёный', tip: 'Есть' } };
            const UNUSED = { a: 'Не выводится' };
            function Row({ row, m }) {
              const flag = row ? FLAGS[row.status] : FLAGS.none;
              const { tip } = flag;
              return <td className={flag.cls} title={tip}>
                {STATUS[row.status] || row.status} {STATUS.wait} {MONTHS[m]} {META[row.type].label}
                {Object.values(STATUS).length} {Object.entries(META).map(([k, v]) => <i key={k}>{v.short}</i>)}
              </td>;
            }
        """,
        "meta.js": """
            export const META = {
              file: { icon: 'значок', label: 'Файл', short: 'Ф' },
              link: { icon: 'значок', label: 'Ссылка', short: 'Сс' },
            };
        """,
    })
    assert got == {"Нет", "Есть", "Готово", "Ждёт", "Январь", "Февраль", "Файл", "Ссылка", "Сс"}


def test_function_returning_object(tmp_path):
    got = texts(tmp_path, {"a.jsx": """
        function chip(answer) {
          switch (answer.status) {
            case 'graded': return { label: `Оценка ${answer.grade}`, cls: 'зелёный' };
            case 'accepted': return { label: 'Принято', cls: 'зелёный' };
            default: return { label: 'Не сдано', cls: 'серый' };
          }
        }
        const Chip = ({ answer }) => {
          const c = chip(answer);
          return <span className={c.cls}>{c.label}</span>;
        };
    """})
    assert got == {"Оценка {{0}}", "Принято", "Не сдано"}


def test_variables_loops_and_additions(tmp_path):
    got = texts(tmp_path, {"a.jsx": """
        function Page({ rows, bad }) {
          const title = bad ? 'Ошибки' : 'Всё хорошо';
          let note;
          note = 'Примечание';
          const hidden = 'Не выводится';
          const lines = ['Первая'];
          lines.push('Вторая', bad ? 'Третья' : 'Четвёртая');
          const byId = {};
          byId[rows[0].id] = 'По ключу';
          const out = [];
          for (const line of lines) out.push(<li>{line}</li>);
          const summary = 'Всего: ' + rows.length + `, плохих: ${bad}`;
          return <div title={title}>{note}{out}{byId[rows[1].id]}{summary}{rows.length + 1}</div>;
        }
    """})
    assert got == {"Ошибки", "Всё хорошо", "Примечание", "Первая", "Вторая", "Третья", "Четвёртая", "По ключу",
                   "Всего: {{0}}, плохих: {{1}}"}


# --- компоненты и функции, переданные дальше --------------------------------

def test_component_props(tmp_path):
    got = texts(tmp_path, {
        "a.jsx": """
            import Field, { Badge } from './field';
            const TIPS = { a: 'Удалить файл', b: 'Удалить ссылку' };
            const table = (rows, opts) => rows.map(r => <Badge key={r.id} text={opts.caption} mode="Служебное" />);
            const Page = ({ kind, err, rows }) => <div>
              <Field error={err ? 'Ссылка должна начинаться с https://' : ''} name="Служебное" hint={TIPS[kind]} />
              <Field {...{ error: 'Из spread' }} />
              {table(rows, { caption: 'Педагог', path: 'служебный путь' })}
            </div>;
        """,
        "field.jsx": """
            export function Badge(props) { const { text } = props; return <b className={props.mode}>{text}</b>; }
            function Field({ error, name, hint = 'Подсказка по умолчанию' }) {
              return <label htmlFor={name}><i title={hint} />{error && <span>{error}</span>}</label>;
            }
            export default Field;
        """,
    }, attrs=["tip"])
    assert got == {"Ссылка должна начинаться с https://", "Удалить файл", "Удалить ссылку", "Из spread", "Педагог",
                   "Подсказка по умолчанию"}


def test_default_prop_is_collected_in_component_file(tmp_path):
    got = texts(tmp_path, {"field.jsx": """
        export default function Field({ hint = 'Подсказка по умолчанию', id = 'служебный' }) {
          return <i id={id} title={hint} />;
        }
    """})
    assert got == {"Подсказка по умолчанию"}


def test_setter_passed_to_another_component(tmp_path):
    files = {
        "child.jsx": """
            import { errorText } from './errors';
            export default function Child({ onError, onDone }) {
              const fail = (err, fallback) => onError(errorText(err, fallback));
              const save = () => {
                if (!ok) { onError('Укажите название'); return; }
                api().then(() => onDone('Не текст')).catch(e => fail(e, 'Не удалось сохранить'));
              };
              return <Inner onError={onError} onClick={save} />;
            }
            const Inner = memo(function Inner({ onError }) {
              return <button onClick={() => onError('Из вложенного')} />;
            });
        """,
        "errors.js": "export const errorText = (err, fallback) => err.detail || fallback;",
        "page.jsx": """
            import Child from './child';
            export default function Page() {
              const [error, setError] = useState('');
              const [, setDone] = useState('');
              return <div>{error}<Child onError={msg => setError(msg)} onDone={setDone} /></div>;
            }
        """,
    }
    assert texts(tmp_path, files) == {"Укажите название", "Не удалось сохранить", "Из вложенного"}


def test_state_updater_and_keyed_errors(tmp_path):
    got = texts(tmp_path, {"a.jsx": """
        function Table({ rows }) {
          const [rowErrors, setRowErrors] = useState({});
          const save = (id) => {
            setRowErrors(prev => ({ ...prev, [id]: '' }));
            api().catch(() => setRowErrors(prev => ({ ...prev, [id]: 'Не удалось сохранить строку' })));
          };
          return rows.map(r => <td key={r.id} onClick={() => save(r.id)}>{rowErrors[r.id]}</td>);
        }
    """})
    assert got == {"Не удалось сохранить строку"}


def test_thrown_error_shown_from_catch(tmp_path):
    got = texts(tmp_path, {"a.jsx": """
        function parse(text) {
          if (!text) throw new Error('Пустой текст');
          return check(JSON.parse(text));
        }
        function check(data) {
          if (!data.list) throw new Error(`Нет списка: ${data.name}`);
          return data;
        }
        function unrelated() { throw new Error('Не показывается'); }
        function Form({ text }) {
          const [error, setError] = useState('');
          const fill = () => {
            try {
              if (text.length > 100) throw { detail: 'Слишком длинно', code: 'служебный' };
              parse(text);
            } catch (e) {
              setError(e.detail || e.message);
            }
          };
          return <p onClick={fill}>{error}</p>;
        }
    """})
    assert got == {"Пустой текст", "Нет списка: {{0}}", "Слишком длинно"}


def test_call_argument_belongs_to_its_own_call(tmp_path):
    got = texts(tmp_path, {"a.jsx": """
        const pick = (value, fallback) => value || fallback;
        const Page = ({ a }) => {
          console.log(pick(a, 'Только в журнал'));
          return <p>{pick(a, 'На экран')}</p>;
        };
    """})
    assert got == {"На экран"}


def test_recursion_and_cycles_do_not_hang(tmp_path):
    got = texts(tmp_path, {"a.jsx": """
        const a = (n) => n ? b(n - 1) : 'Конец';
        const b = (n) => a(n) || loop;
        let loop = loop2; let loop2 = loop;
        const Tree = ({ node }) => <div>{a(3)}{loop}{node.children.map(c => <Tree key={c.id} node={c} />)}</div>;
    """})
    assert got == {"Конец"}


# --- рантайм ----------------------------------------------------------------

def test_runtime_translates_native_dialogs():
    js = build_frontend_runtime_script({})
    assert 'wrapDialog("alert");' in js and 'wrapDialog("confirm");' in js
    assert "args[0] = translateMessage(args[0]);" in js
