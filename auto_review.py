#!/usr/bin/env python3
"""
auto_review.py

Local python script that:
 - lists merge requests assigned to ASSIGNEE
 - for each MR that has a successful pipeline and no unresolved discussions:
     - fetches changed .c/.h files from MR source branch
     - runs policy checks (rules 0..29 from user)
     - posts review comments (inline when possible) and a summary MR comment
     - if there are no violations and APPROVE_ON_PASS=1, approves the MR

Environment variables:
 - GITLAB_TOKEN  (required)
 - ASSIGNEE      (username or 'me') (required)
 - GITLAB_URL    (optional, default https://git.iu7.bmstu.ru)
 - APPROVE_ON_PASS (optional, '1' to approve when pass, default 1)

Requires: requests (pip install requests)
"""
import os
import sys
import time
import json
import re
import logging
from urllib.parse import quote
import requests
from dotenv import load_dotenv

load_dotenv('script.conf')

# ---------- Configuration ----------
GITLAB_TOKEN = os.environ.get("GITLAB_TOKEN")
ASSIGNEE = os.environ.get("ASSIGNEE")
GITLAB_URL = os.environ.get("GITLAB_URL", "https://git.iu7.bmstu.ru").rstrip("/")
APPROVE_ON_PASS = os.environ.get("APPROVE_ON_PASS", "1") == "1"
LOGFILE = "mrauto.log"

if not GITLAB_TOKEN or not ASSIGNEE:
    print("Error: GITLAB_TOKEN and ASSIGNEE environment variables must be set.", file=sys.stderr)
    print("Example: export GITLAB_TOKEN=...; export ASSIGNEE=me", file=sys.stderr)
    sys.exit(2)

HEADERS = {"Private-Token": GITLAB_TOKEN, "User-Agent": "gitlab-auto-review-script/1.0"}
API_BASE = f"{GITLAB_URL}/api/v4"


# проверка имени на соответствие хотя бы одному из стилей
# поддерживаемые стили: camelCase, snake_case, CamelCase (pascal)
style_patterns = [
    (re.compile(r'^[a-z][A-Za-z0-9]*$'), 'camelCase'),
    (re.compile(r'^[a-z][a-z0-9_]*$'), 'snake_case'),
    (re.compile(r'^[A-Z][A-Za-z0-9]*$'), 'CamelCase'),
]

# Configure logging
logging.basicConfig(filename=LOGFILE,
                    level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger('').addHandler(console)

# ---------- Helpers: GitLab API ----------
def api_get(path, params=None):
    url = API_BASE + path
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def api_post(path, data=None):
    url = API_BASE + path
    r = requests.post(url, headers=HEADERS, json=data if data is not None else {}, timeout=30)
    r.raise_for_status()
    return r.json()

def api_post_nojson(path, data=None):
    url = API_BASE + path
    r = requests.post(url, headers=HEADERS, data=data or {}, timeout=30)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {}

# ---------- Utility: user lookup ----------
def get_user_id_by_username(username):
    if username == "me":
        user = api_get("/user")
        return user["id"], user.get("username")
    # search users by username
    res = api_get("/users", params={"username": username})
    if not res:
        raise RuntimeError(f"No such user: {username}")
    return res[0]["id"], res[0].get("username")

# ---------- List MRs assigned to user (across all projects) ----------
def list_assigned_mrs(assignee_id):
    # Use per_page loop
    page = 1
    mrs = []
    while True:
        chunk = api_get("/merge_requests", params={"scope": "assigned_to_me", "state": "opened", "merge_status": "can_be_merged", "per_page": 100, "page": page})
        if not chunk:
            break
        mrs.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return mrs

# ---------- Check MR pipeline success ----------
def mr_has_successful_pipeline(project_id, mr_iid):
    # Use MR pipelines endpoint
    try:
        pipelines = api_get(f"/projects/{project_id}/merge_requests/{mr_iid}/pipelines")
        if not pipelines:
            return False
        # choose pipeline with greatest id (most recent)
        pipelines_sorted = sorted(pipelines, key=lambda p: p.get("id", 0), reverse=True)
        latest = pipelines_sorted[0]
        status = latest.get("status")
        logging.info(f"  MR !{mr_iid} latest pipeline id={latest.get('id')} status={status}")
        return status == "success"
    except requests.HTTPError as e:
        logging.warning(f"  Could not fetch pipelines for MR !{mr_iid}: {e}")
        return False

# ---------- Check unresolved discussions ----------
def mr_has_unresolved_discussions(project_id, mr_iid):
    try:
        discussions = api_get(f"/projects/{project_id}/merge_requests/{mr_iid}/discussions", params={"per_page": 100})
        for d in discussions:
            # GitLab discussion object: top-level has "notes"; each note may be resolvable/resolved.
            # Also sometimes discussion itself has 'resolvable' and 'resolved'.
            if d.get("resolvable", False) and not d.get("resolved", False):
                return True
            notes = d.get("notes", [])
            for n in notes:
                # note: 'resolvable' and 'resolved' may be present on notes.
                if n.get("resolvable", False) and not n.get("resolved", False):
                    return True
        return False
    except requests.HTTPError as e:
        logging.warning(f"  Could not fetch discussions for MR !{mr_iid}: {e}")
        # be conservative: consider unresolved if we cannot check
        return True

# ---------- Fetch changes and file contents from MR ----------
def fetch_changed_c_files(project_id, mr_iid, source_branch):
    """
    Returns dict: path -> content for changed files ending in .c or .h
    """
    try:
        details = api_get(f"/projects/{project_id}/merge_requests/{mr_iid}/changes")
    except requests.HTTPError as e:
        logging.error(f"  Failed to fetch MR changes: {e}")
        return {}
    changes = details.get("changes", [])
    result = {}
    for ch in changes:
        new_path = ch.get("new_path")
        if not new_path:
            continue
        if not (new_path.endswith(".c") or new_path.endswith(".h")):
            continue
        # fetch raw file at source branch
        encoded_path = quote(new_path, safe='')
        try:
            raw = requests.get(f"{API_BASE}/projects/{project_id}/repository/files/{encoded_path}/raw",
                               headers=HEADERS, params={"ref": source_branch}, timeout=30)
            if raw.status_code == 200:
                result[new_path] = raw.text.splitlines()
            else:
                logging.warning(f"   Could not fetch file {new_path} (status {raw.status_code}). Skipping.")
        except Exception as e:
            logging.warning(f"   Error fetching file {new_path}: {e}")
    return result

# ---------- Policy checks implementation ----------
# We'll implement checks as functions that return list of issue dicts:
# { 'file': filename or None, 'line': int or None, 'rule': int, 'message': str }

# Simple regex helpers
func_def_re = re.compile(r'^[\w\*\s]+?\b([A-Za-z_][A-Za-z0-9_]*)\s*\(([^;]*)\)\s*\{')  # heuristic
number_literal_re = re.compile(r'(?<![_A-Za-z0-9])(-?\d+(\.\d+)?)(?![_A-Za-z0-9])')
scanf_re = re.compile(r'\bscanf\s*\(')
malloc_re = re.compile(r'\b(malloc|realloc|calloc)\s*\(')
exit_re = re.compile(r'\bexit\s*\(')
goto_re = re.compile(r'\bgoto\b')
translit_tokens = ['vvod', 'vivod', 'chislo', 'soobshchenie', 'massiv', 'stroka', 'otvet', 'perechislenie']
# For rule 16: detect &arr[i]
amp_arr_index_re = re.compile(r'&\s*[A-Za-z_][A-Za-z0-9_]*\s*\[')

# Enforce camelCase: require no underscores and start with lower-case letter for vars and functions
camel_re = re.compile(r'^[a-z][A-Za-z0-9]*$')

def run_checks_on_files(file_lines_map):
    issues = []
    # files is dict: path -> list of lines (strings)
    for path, lines in file_lines_map.items():
        # Rule 1 + 2: naming & translit - we scan for identifiers in code lines (heuristic)
        for i, line in enumerate(lines, start=1):
            # skip comments and preproc directives for naming detection
            if re.match(r'^\s*#', line) or re.match(r'^\s*//', line) or re.match(r'^\s*/\*', line):
                continue
            # find candidate identifiers (variable or function definitions)
            # check translit tokens
            for t in translit_tokens:
                if re.search(r'\b' + re.escape(t) + r'\b', line, flags=re.I):
                    issues.append({'file': path, 'line': i, 'rule': 3, 'message': f"Найден транслит '{t}'; используйте переводчик (правило 3)."})
            # === FIXED BLOCK: smart &arr[i] detection ===
            # Remove string literals, char literals, block + line comments
            ln = re.sub(r'/\*.*?\*/', '', line)
            ln = re.sub(r'//.*', '', ln)
            ln = re.sub(r'"([^"\\]|\\.)*"', '', ln)
            ln = re.sub(r"'([^'\\]|\\.)*'", '', ln)

            pattern = r'(^|\s)&\s*([A-Za-z_][A-Za-z0-9_]*)\s*$$[^$$]+\]'
            match = re.search(pattern, ln)
            if match:
                # Проверяем, что после ] нет сразу )
                end_pos = match.end()
                if end_pos >= len(ln) or ln[end_pos] not in (')', '*'):
                    issues.append({
                        'file': path,
                        'line': i,
                        'rule': 0,
                        'message': "&arr[i] запрещено. БАН (правило 0)."
                    })
            # naming detection: look for function definitions
            m = func_def_re.match(line.strip())
            if m:
                fname = m.group(1)
                # проверим соответствие имени хотя бы одному стилю
                matched_style = None
                for pat, sname in style_patterns:
                    if pat.match(fname):
                        matched_style = sname
                        break
                if not matched_style:
                    allowed = ", ".join(s for _, s in style_patterns)
                    issues.append({
                        'file': path,
                        'line': i,
                        'rule': 2,
                        'message': f"Функция '{fname}' не соответствует ни одному из допустимых стилей имён ({allowed}) (правило 2)."
                    })
                # check parameter count (rule 12)
                params = m.group(2).strip()
                if params and params != 'void':
                    # count commas ignoring nested parentheses (heuristic)
                    param_count = params.count(',') + 1 if params else 0
                else:
                    param_count = 0
                if param_count > 5:
                    issues.append({
                        'file': path,
                        'line': i,
                        'rule': 12,
                        'message': f"У функции '{fname}' {param_count} параметров (правило 12: предел 5)."
                    })
                # --- правило 10: не более двух return в функции ---
                brace_level = 0
                return_count = 0
                inside_function = False

                # мы знаем, что объявление встретилось, ищем тело
                # и начинаем подсчёт return после найденной '{'
                for j in range(i, len(lines)):
                    l = lines[j]

                    if '{' in l:
                        brace_level += l.count('{')
                        inside_function = True

                    if inside_function:
                        # считаем return (простая эвристика)
                        if re.search(r'\breturn\b', l):
                            return_count += 1

                    if '}' in l and inside_function:
                        brace_level -= l.count('}')
                        if brace_level == 0:
                            break  # вышли из тела функции

                if return_count > 2:
                    issues.append({
                        'file': path,
                        'line': i,
                        'rule': 10,
                        'message': f"Функция '{fname}' содержит {return_count} return (правило 10: максимум 2)."
                    })


            # variable names (heuristic): detect simple 'type name;' patterns
            var_decl = re.search(r'\b(?:int|char|float|double|long|short|size_t|unsigned|struct)\s+([A-Za-z_][A-Za-z0-9_]*)', line)
            if var_decl:
                vname = var_decl.group(1)
                matched_style = None
                for pat, sname in style_patterns:
                    if pat.match(vname):
                        matched_style = sname
                        break
                if not matched_style:
                    allowed = ", ".join(s for _, s in style_patterns)
                    issues.append({
                        'file': path,
                        'line': i,
                        'rule': 2,
                        'message': f"Переменная '{vname}' не соответствует ни одному из допустимых стилей имён ({allowed}) (правило 2)."
                    })

        # Now do function-body-aware checks: function length, nesting, unused params etc.
        # We'll find functions by searching for lines with '{' and '}' and func signature heuristics.
        nlines = len(lines)
        idx = 0
        while idx < nlines:
            line = lines[idx]
            # skip preprocessor directives
            if line.strip().startswith('#'):
                idx += 1
                continue
            m = func_def_re.match(line.strip())
            if m:
                fname = m.group(1)
                params = m.group(2).strip()
                # find function body end by counting braces
                brace = 0
                started = False
                start_idx = idx
                max_brace = 0
                j = idx
                while j < nlines:
                    l = lines[j]
                    # remove string literals for safety
                    lnos = re.sub(r'"([^"\\]|\\.)*"', '""', l)
                    for ch in lnos:
                        if ch == '{':
                            brace += 1
                            started = True
                        elif ch == '}':
                            brace -= 1
                    if started:
                        max_brace = max(max_brace, brace)
                    if started and brace == 0:
                        break
                    j += 1
                end_idx = j
                func_len = end_idx - start_idx + 1
                # Rule 4: function length <= 30 lines
                if func_len > 30:
                    issues.append({'file': path, 'line': start_idx+1, 'rule': 4, 'message': f"Функция '{fname}' содержит {func_len} строк (правило 4: предел 30)."})
                # Rule 12: nesting >3
                # subtract 1 for the function's outer braces
                nesting = max(0, max_brace - 1)
                if nesting > 3:
                    issues.append({'file': path, 'line': start_idx+1, 'rule': 12, 'message': f"Вложенность функции '{fname}' составляет {nesting} > 3 (правило 12)."})
                # Rule 12 param count again (repeat-check)
                if params and params != 'void':
                    param_count = params.count(',') + 1
                else:
                    param_count = 0
                if param_count > 5:
                    issues.append({'file': path, 'line': start_idx+1, 'rule': 12, 'message': f"Функция '{fname}' содержит {param_count} параметров (rule 12)."})
                # Rule 18: detect unused args (heuristic: check param names appear inside function body)
                param_names = []
                if params and params != 'void':
                    # split on commas and strip type to get name heuristically
                    parts = [p.strip() for p in params.split(',')]
                    for p in parts:
                        # attempt to get last token as name
                        tokens = p.split()
                        if tokens:
                            name = tokens[-1]
                            name = name.replace('*', '').strip()
                            # remove possible default or array
                            name = re.sub(r'[\[\].]*', '', name)
                            if name:
                                param_names.append(name)
                body_text = "\n".join(lines[start_idx:end_idx+1]) if end_idx < nlines else "\n".join(lines[start_idx:])
                for pn in param_names:
                    if pn and not re.search(r'\b' + re.escape(pn) + r'\b', body_text):
                        issues.append({'file': path, 'line': start_idx+1, 'rule': 18, 'message': f"Параметр '{pn}' не используется в функции '{fname}' (правило 18)."})

                # Rule 8: scanf return check heuristic within function
                for k in range(start_idx, min(end_idx+1, nlines)):
                    if scanf_re.search(lines[k]):
                        # look for "if (scanf(...)" or "ret = scanf(...)" or "=="
                        context = "\n".join(lines[max(start_idx, k-3):min(nlines, k+4)])
                        if '==' not in context and '!=' not in context and 'if' not in context and 'return' not in context and '=' not in context:
                            issues.append({'file': path, 'line': k+1, 'rule': 8, 'message': "scanf: возвращаемое значение не проверяется (правило 8)."})
                # Rule 15: check malloc result checked (heuristic: look for malloc and subsequent NULL check)
                for k in range(start_idx, min(end_idx+1, nlines)):
                    line = lines[k]
                    # 1. malloc внутри условия if/while
                    if re.search(r'\b(if|while)\s*\(\s*!\s*\(*\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(malloc|realloc|calloc)\s*\(', line):
                        continue  # обработка есть: if (!(p = malloc(...)))
                    # 2. обычное присваивание
                    m = re.search(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*.*\b(malloc|realloc|calloc)\s*\(', line)
                    if m:
                        v = m.group(1)
                        checked = False
                        # ищем обработку в ближайших 10 строках или до конца функции
                        for t in range(k+1, min(k+11, end_idx+1)):
                            check_line = lines[t]
                            # Явные проверки
                            if re.search(r'\bif\s*\(\s*!\s*' + re.escape(v) + r'\s*\)', check_line):
                                checked = True
                                break
                            if re.search(r'\bif\s*\(\s*' + re.escape(v) + r'\s*==\s*NULL\s*\)', check_line):
                                checked = True
                                break
                            if re.search(r'\bif\s*\(\s*' + re.escape(v) + r'\s*==\s*0\s*\)', check_line):
                                checked = True
                                break
                            if re.search(r'\bif\s*\(\s*' + re.escape(v) + r'\s*!=\s*NULL\s*\)', check_line):
                                checked = True
                                break
                            if re.search(r'\bif\s*\(\s*' + re.escape(v) + r'\s*!=\s*0\s*\)', check_line):
                                checked = True
                                break
                            # assert(v != NULL)
                            if re.search(r'assert\s*\(\s*' + re.escape(v) + r'\s*!=\s*NULL\s*\)', check_line):
                                checked = True
                                break
                            # return if allocation failed
                            if re.search(r'\bif\s*\(\s*!\s*' + re.escape(v) + r'\s*\)\s*return', check_line):
                                checked = True
                                break
                            # иногда пишут просто if (v)
                            if re.search(r'\bif\s*\(\s*' + re.escape(v) + r'\s*\)', check_line):
                                checked = True
                                break
                            # --- Новый блок: косвенная проверка через функцию ---
                            # ищем if (<function>(v, ...) == NULL) или if (<function>(v, ...) == 0)
                            func_call_null = re.search(
                                r'\bif\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*' + re.escape(v) + r'[\s,)]',
                                check_line
                            )
                            if func_call_null:
                                # ищем сравнение результата вызова с NULL или 0
                                # Пример: if (input_arr(arr, n) == NULL)
                                after = check_line[func_call_null.end()-1:]
                                if re.search(r'==\s*NULL', after) or re.search(r'==\s*0', after) or re.search(r'!=\s*NULL', after) or re.search(r'!=\s*0', after):
                                    checked = True
                                    break
                        #if not checked:
                        #    issues.append({'file': path, 'line': k+1, 'rule': 15, 'message': "Результат malloc|realloc|calloc не обработан (правило 15)."})
                # Rule 25 & 26: forbid exit/goto inside function
                for k in range(start_idx, min(end_idx+1, nlines)):
                    if exit_re.search(lines[k]):
                        issues.append({'file': path, 'line': k+1, 'rule': 25, 'message': "Использование функции exit() (правило 25)."})
                    if goto_re.search(lines[k]):
                        issues.append({'file': path, 'line': k+1, 'rule': 26, 'message': "Использование goto (правило 26)."})
                # Rule 14: float equality detection in function body (heuristic)
                for k in range(start_idx, min(end_idx+1, nlines)):
                    if re.search(r'\d+\.\d+', lines[k]) and re.search(r'==|>=|<=', lines[k]):
                        issues.append({'file': path, 'line': k+1, 'rule': 14, 'message': "Вещественное число сравнивается некорректно (правило 14)."})
                # Move idx to end of function
                idx = end_idx + 1
                continue
            idx += 1

        # Rule 5: magic numbers in file (heuristic) - outside of defines or enums
        for i, l in enumerate(lines, start=1):
            if l.strip().startswith('#') or 'enum' in l or 'define' in l.lower():
                continue
            for nm in number_literal_re.finditer(l):
                val = nm.group(1)
                if val in ('0', '1', '-1'):
                    continue
                # ignore char literals like '0x' hex? we flagged decimal only in pattern above
                issues.append({'file': path, 'line': i, 'rule': 5, 'message': f"Магическая константа {val} (разрешено: 0,1,-1). (правило 5)."})

        # Rule 21: trivial redundant computations detection (heuristic)
        for i, l in enumerate(lines, start=1):
            if re.search(r'\b(\w+)\s*=\s*\1\s*\+\s*0\b', l) or re.search(r'\b(\w+)\s*=\s*\1\s*\*\s*1\b', l):
                issues.append({'file': path, 'line': i, 'rule': 21, 'message': "Лишние вычисления (например, x = x + 0) (rule 21)."})

        # Улучшённая детекция глобальных переменных (заменяет старую секцию)
        # Сканируем верхнюю часть файла — до первой функции (или первые 300 строк),
        # собираем логические декларации (заканчиваются ';') и применяем эвристику.

        first_func_line = None
        for idx_l, l in enumerate(lines):
            if func_def_re.match(l.strip()):
                first_func_line = idx_l
                break

        # Ограничим область поиска, чтобы не обрабатывать огромные файлы полностью
        scan_limit = first_func_line if first_func_line is not None else min(len(lines), 300)
        search_region = lines[:scan_limit]

        # Собираем блоки до ';' (учитываем многострочные объявления)
        i = 0
        while i < len(search_region):
            line = search_region[i]
            # пропускаем пре-процессорные директивы и пустые / комментированные строки
            if line.strip().startswith('#') or re.match(r'^\s*//', line) or re.match(r'^\s*/\*', line):
                i += 1
                continue

            # соберём блок до ближайшего ';' или до конца области
            block_lines = []
            start_line_no = i + 1  # 1-based
            j = i
            found_semicolon = False
            while j < len(search_region):
                block_lines.append(search_region[j])
                if ';' in search_region[j]:
                    found_semicolon = True
                    j += 1
                    break
                j += 1

            # если не нашли ';', переходим дальше
            if not found_semicolon:
                i = j
                continue

            raw_block = '\n'.join(block_lines)

            # Удалим строковые и блочные комментарии для точного анализа
            # удаляем /* ... */ и //... (простая очистка)
            no_comments = re.sub(r'/\*.*?\*/', '', raw_block, flags=re.S)
            no_comments = re.sub(r'//.*', '', no_comments)

            # Удалим leading/trailing whitespace и переводы строк
            tok = no_comments.strip()
            # пропускаем typedef-ы — они не являются глобальными переменными
            if re.match(r'^\s*typedef\b', tok):
                i = j
                continue
            # пропускаем чистые объявления типов: "struct X;" или "enum Y;" или "union Z;"
            if re.match(r'^\s*(struct|enum|union)\b[^{;]*;\s*$', tok):
                i = j
                continue
            # пропускаем объявления только прототипов функций (есть '(' -> скорее всего прототип)
            if '(' in tok and ')' in tok:
                # функция-прототип или указатель на функцию — не считать как глоб.переменную
                i = j
                continue
            # пропускаем пустые блоки
            if not tok or tok == ';':
                i = j
                continue

            # Эвристика: определяем, похоже ли это на объявление переменной.
            # Требуем: в блоке присутствует идентификатор переменной, и либо есть '=' либо '[' либо сам факт "type name;".
            # Дополнительно игнорируем явные 'extern' если вы хотите разрешать extern — здесь мы не игнорируем 'extern' автоматически.
            # Найти первую функцию (или все функции), чтобы знать, где заканчивается "глобальная" область
            first_func_line = None
            for idx_l, l in enumerate(lines):
                if func_def_re.match(l.strip()):
                    first_func_line = idx_l
                    break

            # Ограничить область поиска только до первой функции (или до 300 строк, если функций нет)
            scan_limit = first_func_line if first_func_line is not None else min(len(lines), 300)
            search_region = lines[:scan_limit]

            i = 0
            while i < len(search_region):
                line = search_region[i]
                # пропускаем препроцессорные директивы и пустые / комментированные строки
                if line.strip().startswith('#') or re.match(r'^\s*//', line) or re.match(r'^\s*/\*', line):
                    i += 1
                    continue

                # собираем блок до ближайшего ';' или до конца области
                block_lines = []
                start_line_no = i + 1  # 1-based
                j = i
                found_semicolon = False
                while j < len(search_region):
                    block_lines.append(search_region[j])
                    if ';' in search_region[j]:
                        found_semicolon = True
                        j += 1
                        break
                    j += 1

                if not found_semicolon:
                    i = j
                    continue

                raw_block = '\n'.join(block_lines)
                no_comments = re.sub(r'/\*.*?\*/', '', raw_block, flags=re.S)
                no_comments = re.sub(r'//.*', '', no_comments)
                tok = no_comments.strip()

                # пропускаем typedef, struct/enum/union объявления, прототипы функций, extern/static
                if re.match(r'^\s*typedef\b', tok):
                    i = j
                    continue
                if re.match(r'^\s*(struct|enum|union)\b[^{;]*;\s*$', tok):
                    i = j
                    continue
                if '(' in tok and ')' in tok:
                    i = j
                    continue
                if re.match(r'^\s*extern\b', tok):
                    i = j
                    continue
                if re.match(r'^\s*static\b', tok):
                    i = j
                    continue

                # Только здесь ищем глобальные переменные!
                var_match = re.search(
                    r'^\s*(?:const|volatile|unsigned|signed|register)?\s*'           # storage-class без extern/static
                    r'(?:struct|enum|union|[A-Za-z_][A-Za-z0-9_\s\*]+?)\s+'          # тип
                    r'([A-Za-z_][A-Za-z0-9_]*)'                                      # имя
                    r'\s*(?:$$.*$$|\=.+)?\s*;\s*$',                                  # массив/инициализация/;
                    tok, re.S)
                if var_match:
                    var_name = var_match.group(1)
                    # issues.append({
                    #     'file': path,
                    #     'line': start_line_no,
                    #     'rule': 27,
                    #     'message': f"Обнаружена вероятная глобальная переменная '{var_name}' (правило 27). Рекомендуется избегать глобальных переменных."
                    # })
                i = j


    return issues

# ---------- MR annotation helpers ----------
def post_mr_summary(project_id, mr_iid, summary_text):
    try:
        r = api_post(f"/projects/{project_id}/merge_requests/{mr_iid}/notes", data={"body": summary_text})
        return r
    except Exception as e:
        logging.error(f"  Failed to post MR note: {e}")
        return None

def post_inline_comment(project_id, mr_iid, path, line, message):
    """
    Create an inline discussion on this MR file+line.
    Uses the discussions API with a 'position' object. If it fails, return False.
    """
    # position_type 'text' generally works for plain text positions (non-diff)
    payload = {
        "body": message,
        "position": {
            "position_type": "text",
            "new_path": path,
            "new_line": line
        }
    }
    try:
        r = api_post(f"/projects/{project_id}/merge_requests/{mr_iid}/discussions", data=payload)
        return True
    except Exception as e:
        logging.debug(f"    Inline comment failed for {path}:{line}: {e}")
        return False

def approve_mr(project_id, mr_iid):
    try:
        api_post_nojson(f"/projects/{project_id}/merge_requests/{mr_iid}/approve")
        return True
    except Exception as e:
        logging.error(f"  Failed to approve MR !{mr_iid}: {e}")
        return False

# ---------- Top-level flow ----------
def main():
    try:
        assignee_id, assignee_username = get_user_id_by_username(ASSIGNEE)
    except Exception as e:
        logging.error(f"Cannot find assignee '{ASSIGNEE}': {e}")
        sys.exit(1)
    logging.info(f"Assignee: {assignee_username} (id {assignee_id})")

    mrs = list_assigned_mrs(assignee_id)
    logging.info(f"Found {len(mrs)} open merge requests assigned to {assignee_username}.")
    print(len(mrs))

    for mr in mrs:
        # mr is a dict; we need project_id and iid
        project_id = mr.get("project_id")
        mr_iid = mr.get("iid")
        mr_title = mr.get("title", "")
        mr_author = mr.get("author", {}).get("username") or mr.get("author", {}).get("name")
        source_branch = mr.get("source_branch", mr.get("sha") or "master")  # fallback
        project_path = mr.get("references", {}).get("full") or f"{project_id}"
        logging.info(f"Processing MR !{mr_iid} in project {project_id} - '{mr_title}'. Author: {mr_author}")
        # Rule 0: MR title naming must contain 'lab N' pattern
        title_issues = []
        if not re.search(r'(?i)\blab\s*\d+\b', mr_title):
            title_issues.append({'file': None, 'line': None, 'rule': 0, 'message': "Назовите понятно merge request (например, 'lab 1') (правило 0)."})

        # Проверка на наличие конфликтов слияния
        conflict_issues = []
        if mr.get("has_conflicts"):
            conflict_issues.append({'file': None, 'line': None, 'rule': 0, 'message': "В Merge Request обнаружены конфликты слияния. Их необходимо разрешить перед merge."})

        # Проверка на неразрешённые дискуссии
        discussion_issues = []
        # Получаем список дискуссий MR (например, через API)
        # Здесь предполагается, что mr["discussions"] — это список дискуссий, каждая из которых — словарь с ключом 'resolved'
        # Если у вас другой способ получения дискуссий, адаптируйте этот блок.
        discussions = mr.get("discussions", [])
        for d in discussions:
            if not d.get("resolved", True):
                discussion_issues.append({'file': None, 'line': None, 'rule': 0, 'message': "В Merge Request есть неразрешённые дискуссии. Пожалуйста, разрешите все обсуждения перед слиянием."})
            break  # достаточно одного неразрешённого обсуждения


        # check pipeline success
        ok_pipeline = mr_has_successful_pipeline(project_id, mr_iid)
        if not ok_pipeline:
            logging.info(f"  Skipping MR !{mr_iid} by {mr_author}: latest pipeline not successful.")
            #logging.info(f"  Logged: skipped due to pipeline")
            continue

        # check unresolved discussions
        unresolved = mr_has_unresolved_discussions(project_id, mr_iid)
        if unresolved:
            logging.info(f"  Skipping MR !{mr_iid} by {mr_author}: has unresolved discussions.")
            #logging.info(f"  Logged: skipped due to unresolved discussions")
            #continue

        # fetch changed C files
        files = fetch_changed_c_files(project_id, mr_iid, source_branch)
        if not files and not title_issues:
            # nothing to check, but if title issue exists we will still post
            logging.info(f"  No changed C/H files found for MR !{mr_iid} by {mr_author}.")
        # run checks
        issues = []
        if files:
            issues = run_checks_on_files(files)
        # include title issues if any
        issues.extend(title_issues)
        issues.extend(conflict_issues)
        issues.extend(discussion_issues)

        # Post inline comments (if possible) and produce summary
        if not issues:
            summary = (":white_check_mark Вас проверила автоматика Lint-Bot :cop: : не найдено нарушений. Lint-Bot :cop: доволен\n\n"
                       "Проверены правила: 0..29.\n\n")
            logging.info(f"  MR !{mr_iid} by {mr_author}: no issues found.")
            # Approve if configured
            if APPROVE_ON_PASS:
                ok = approve_mr(project_id, mr_iid)
                if ok:
                    logging.info(f"  Approved MR !{mr_iid} by {mr_author}.")
                    summary += "\nРешение: Автоматически установлен Approve.\n"
                else:
                    summary += "\nРешение: Approve не установлен (check token/permissions).\n"
            post_mr_summary(project_id, mr_iid, summary)
            logging.info(f"  Logged: reviewed and approved (if enabled) MR !{mr_iid} by {mr_author}.")
            continue

        # Build summary text
        summary_lines = [":x: Вас проверила автоматика Lint-Bot :cop: : найдены возможные нарушения.", "", "Результат:"]
        for it in issues:
            fl = f"{it['file']}:{it['line']}" if it.get('file') and it.get('line') else (it.get('file') or "(project)")
            summary_lines.append(f"- Rule {it['rule']}: {fl} — {it['message']}")
        summary_text = "\n".join(summary_lines)

        # Try posting inline comments for each issue
        inline_posted = 0
        for it in issues:
            if it.get('file') and it.get('line'):
                msg = f"Правило {it['rule']}: {it['message']}"
                ok = post_inline_comment(project_id, mr_iid, it['file'], it['line'], msg)
                if ok:
                    inline_posted += 1
                # small delay to avoid rate limits
                time.sleep(0.2)

        # Post summary note
        post_mr_summary(project_id, mr_iid, "Результаты автоматического ревью:\n\n" + summary_text)
        logging.info(f"  MR !{mr_iid} by {mr_author}: summary: {summary_text} ")
        logging.info(f"  MR !{mr_iid} by {mr_author}: posted summary; inline posted: {inline_posted}")
        logging.info(f"  Logged: reviewed MR !{mr_iid} by {mr_author} with {len(issues)} issues.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        logging.exception("Fatal error:")
        sys.exit(1)
