#!/usr/bin/env python3
"""19.10 (решение Alex: «запускай python для gemma» — бесплатный тренажёр скилла, пока ждём сброса лимита Gemini 24.10).
Скилл на локальной Gemma (Ollama на ПК Alex, OLLAMA_URL, модель GEMMA_MODEL) с инструментом bash, как у agy/Claude:
Gemma пишет команду → команда выполняется в той же изолированной коробке weather-lab-agybox, что agy_box.sh, но ещё и без сети
(--network none): видна только рабочая папка ($2 → /work), корень только для чтения, без root, предел памяти/процессов/времени.
Ollama получает только письмо и вывод команд (данные погоды, без ключей и личных данных).
19.10 (Alex «прогнать через DeepSeek… сделаем таблицу»): LLM_BACKEND=openrouter — та же коробка и тот же скилл, модель GEMMA_MODEL
через OpenRouter (ключ — OPENROUTER_API_KEY из .env, наружу уходят только письмо и вывод команд); на балансе < $5 — не запускается
(остаток держит живые llm_gem/llm_ds до 31.10). Цена письма — "cost" в ответе.
Запуск: gemma_box.py <файл письма> <рабочая папка> [секунд] → JSON {"status", "response", "turns", "tokens", "calls"} в stdout.
20.10 (Alex: «хочу глазами видеть, что происходит, в live режиме»): GEMMA_TRACE=<файл .jsonl> — каждый шаг сразу дописывается строкой
{"t", "turn", "kind", "text"}: think (размышление), text (текст модели), call (команда), result (вывод), empty / fix / parse_fix
(повторы обвязки), final, error. Читают /skill (терминал) и разбор писем; файл остаётся после письма.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

BACKEND = os.environ.get("LLM_BACKEND", "ollama")
URL = (os.environ.get("OLLAMA_URL", "http://192.168.1.16:11434") + "/api/chat" if BACKEND == "ollama"
       else "https://openrouter.ai/api/v1/chat/completions")
OR_MIN_BALANCE = 5.0
# как у llm_ds (weather_llm_hour.PROVIDER_DS) + только провайдеры, умеющие инструменты
OR_PROVIDER = {"order": ["streamlake"], "allow_fallbacks": True, "max_price": {"completion": 3}, "require_parameters": True}
MODEL = os.environ.get("GEMMA_MODEL", "gemma4:26b")
MAX_TURNS = int(os.environ.get("GEMMA_TURNS", "30"))
OUT_MAX = 6000                       # символов вывода команды обратно в разговор
TOOLS = [{"type": "function", "function": {
    "name": "bash",
    "description": ("Run a shell command in the skill folder (/work) and get its output. Python 3 with numpy and pandas is "
                    "available (python3 -c '...' or write a .py file and run it). Each call is a fresh shell: variables do not "
                    "persist, files written in /work do. No network."),
    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}]


TRACE = os.environ.get("GEMMA_TRACE")
TRACE_MAX = 4000                     # символов одного шага в записи


def tr(turn, kind, text):
    if not TRACE or text is None:
        return
    text = str(text)
    try:
        with open(TRACE, "a") as f:
            f.write(json.dumps({"t": round(time.time(), 1), "turn": turn, "kind": kind,
                                "text": text if len(text) <= TRACE_MAX else text[:TRACE_MAX] + f" …[{len(text)} симв.]"},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass


def sh(cmd, work):
    docker = os.environ.get("DOCKER", "docker").split()
    uid = f"{os.environ.get('AGY_UID', os.getuid())}:{os.environ.get('AGY_GID', os.getgid())}"
    try:
        p = subprocess.run([*docker, "run", "--rm", "-i", "--user", uid, "--read-only", "--tmpfs", "/tmp:size=64m",
                            "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                            "--memory", "1g", "--pids-limit", "256", "-v", f"{os.path.abspath(work)}:/work", "-w", "/work",
                            "--entrypoint", "timeout", "weather-lab-agybox", "90", "sh", "-c", cmd],
                           capture_output=True, text=True, timeout=150)
        out = (p.stdout + (("\n[stderr]\n" + p.stderr) if p.stderr.strip() else "")).strip() or f"(no output, exit {p.returncode})"
    except subprocess.TimeoutExpired:
        out = "(timeout)"
    return out if len(out) <= OUT_MAX else out[:OUT_MAX] + f"\n…[cut, {len(out)} chars]"


def or_key():
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    for line in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
        if line.startswith("OPENROUTER_API_KEY="):
            return line.split("=", 1)[1].strip()


def or_balance():
    req = urllib.request.Request("https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {or_key()}"})
    d = json.load(urllib.request.urlopen(req, timeout=30))["data"]
    return d["total_credits"] - d["total_usage"]


def chat_or(messages):
    """OpenRouter (формат OpenAI) → ответ в виде Ollama: аргументы инструмента — словарь, eval_count, cost."""
    body = {"model": MODEL, "messages": messages, "tools": TOOLS, "temperature": 1.0, "provider": OR_PROVIDER,
            "usage": {"include": True}}
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json",
                                                                  "Authorization": f"Bearer {or_key()}"})
    r = json.load(urllib.request.urlopen(req, timeout=600))
    if "choices" not in r:
        raise RuntimeError(str(r.get("error", r))[:300])
    raw = r["choices"][0]["message"]
    m = {"role": "assistant", "content": raw.get("content") or "", "_raw": raw}
    if raw.get("tool_calls"):
        m["tool_calls"] = []
        for tc in raw["tool_calls"]:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            m["tool_calls"].append({"id": tc.get("id"), "function": {"name": tc["function"].get("name"), "arguments": args}})
    u = r.get("usage") or {}
    return {"message": m, "eval_count": u.get("completion_tokens") or 0, "cost": u.get("cost") or 0.0,
            "provider": r.get("provider")}


def chat(messages, think=None):
    if BACKEND == "openrouter":
        return chat_or(messages)
    body = {"model": MODEL, "messages": messages, "tools": TOOLS, "stream": False,
            # 19.10 g1: настройки Google для Gemma (temperature 1.0, top_p 0.95, top_k 64) вместо 0.2 — при низкой температуре
            # «размышление» зацикливалось (Tokyo 11.10: 30 697 токенов за один ход — весь контекст, ответ пустой);
            # num_predict — предел одного хода, чтобы размышление не съедало контекст
            # 19.10 Qwen3 thinking: свои настройки от Qwen (0.6 / 0.95 / 20) — GEMMA_TEMP / GEMMA_TOP_P / GEMMA_TOP_K
            "options": {"num_ctx": int(os.environ.get("GEMMA_CTX", "32768")), "temperature": float(os.environ.get("GEMMA_TEMP", "1.0")),
                        "top_p": float(os.environ.get("GEMMA_TOP_P", "0.95")), "top_k": int(os.environ.get("GEMMA_TOP_K", "64")),
                        "num_predict": int(os.environ.get("GEMMA_PREDICT", "6000"))}}
    if think is not None:        # 20.10 g2: после пустого ответа — без «размышления», сразу текст
        body["think"] = think
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    for attempt in range(3):     # 19.10 gpt-oss: Ollama изредка отвечает 500 — повтор хода, в ошибке текст ответа Ollama
        try:
            return json.load(urllib.request.urlopen(req, timeout=1200))
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}: {e.read()[:300].decode(errors='replace')}"
            if e.code < 500 or attempt == 2:
                raise RuntimeError(err) from None
            time.sleep(5)


def buckets(prompt):
    """Названия вариантов из письма (блок «Market buckets…», строки «<вариант>: <цена>¢»)."""
    part = prompt.split("Market buckets", 1)[1] if "Market buckets" in prompt else ""
    return re.findall(r"^(.+?): [\d.]+¢\s*$", part, re.M)


def bad_veto(text):
    """20.10 (заметки #92 / #95): ответ вето — {"p_win": 0-1, "cancel": true/false, "why": ...}."""
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        a = json.loads(m.group(0)) if m else None
    except ValueError:
        a = None
    if not isinstance(a, dict) or not isinstance(a.get("cancel"), bool):
        return "there is no JSON object with a boolean \"cancel\""
    try:
        return None if 0 <= float(a.get("p_win")) <= 1 else "\"p_win\" must be between 0 and 1"
    except (TypeError, ValueError):
        return "\"p_win\" must be a number between 0 and 1"


def bad_answer(text, labels=()):
    """19.10 g1: что не так с итоговым ответом (None — годится). Gemma отвечала без probs (Miami 12.10: {"prediction": ...}).
    20.10: и названия вариантов — как в письме (gpt-oss Tokyo 12.10: свои «30+» — выигравший вариант получил 0)."""
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        p = json.loads(m.group(0))["probs"] if m else None
    except (ValueError, KeyError, TypeError):
        p = None
    if not isinstance(p, dict) or len(p) < 2:
        return "there is no valid \"probs\" object with every bucket of the letter"
    try:
        s = sum(float(v) for v in p.values())
    except (TypeError, ValueError):
        return "some \"probs\" values are not numbers"
    wrong, miss = [k for k in p if k not in labels], [k for k in labels if k not in p]
    if labels and (wrong or miss):
        return (f"\"probs\" must use exactly the bucket labels of the letter: {', '.join(labels)}"
                + (f"; unknown labels: {', '.join(wrong)}" if wrong else "") + (f"; missing: {', '.join(miss)}" if miss else ""))
    return None if 0.97 <= s <= 1.03 else f"\"probs\" sum to {s:.2f}, they must sum to 1"


def main():
    prompt, work = open(sys.argv[1]).read(), sys.argv[2]
    deadline = time.time() + int(sys.argv[3]) if len(sys.argv) > 3 else None
    msgs = [{"role": "user", "content": prompt}]
    labels = buckets(prompt)
    veto = "PROPOSED BET" in prompt      # 20.10: письмо вето — другой ответ (bad_veto)
    tokens = calls = empty = fixes = parse_fix = 0
    cost, providers = 0.0, set()
    tr(0, "start", f"{BACKEND} {MODEL}")
    if BACKEND == "openrouter":
        try:
            bal = or_balance()
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"status": "ERROR", "error": f"баланс OpenRouter не прочитан: {str(e)[-200:]}", "turns": 0}))
            return
        if bal < OR_MIN_BALANCE:
            print(json.dumps({"status": "ERROR", "error": f"BUDGET: на OpenRouter ${bal:.2f} < ${OR_MIN_BALANCE:.0f} — стоп", "turns": 0}))
            return
    for turn in range(1, MAX_TURNS + 1):
        try:
            r = chat(msgs, think=False if empty else None)
        except RuntimeError as e:
            if "error parsing tool call" in str(e) and parse_fix < 3:
                parse_fix += 1
                tr(turn, "parse_fix", str(e)[-300:])  # 19.10 gpt-oss: пишет итог в вызов инструмента — Ollama не разбирает; подсказать, как ответить
                msgs.append({"role": "user", "content": "Your last message could not be parsed. If you are done, reply with "
                             "ONLY the final JSON object as plain text (no tool call). Otherwise call the bash tool with "
                             "valid JSON arguments {\"command\": \"...\"}."})
                continue
            tr(turn, "error", str(e)[-300:])
            print(json.dumps({"status": "ERROR", "error": str(e)[-300:], "turns": turn, "tokens": tokens, "calls": calls,
                              "cost": round(cost, 4)}))
            return
        except Exception as e:  # noqa: BLE001
            tr(turn, "error", str(e)[-300:])
            print(json.dumps({"status": "ERROR", "error": str(e)[-300:], "turns": turn, "tokens": tokens, "calls": calls,
                              "cost": round(cost, 4)}))
            return
        m = r["message"]
        tokens += r.get("eval_count") or 0
        cost += r.get("cost") or 0.0
        if r.get("provider"):
            providers.add(r["provider"])
        tr(turn, "think", m.get("thinking") or (m.get("_raw") or {}).get("reasoning") or None)
        tr(turn, "text", (m.get("content") or "").strip() or None)
        if "_raw" in m:          # OpenRouter: сообщение как пришло (id вызовов, рассуждение для следующего хода)
            msgs.append({k: v for k, v in m["_raw"].items() if k in ("role", "content", "tool_calls", "reasoning_details")})
        else:
            msgs.append({k: m[k] for k in ("role", "content", "tool_calls") if k in m})
        if not m.get("tool_calls") and not (m.get("content") or "").strip() and empty < 2:
            empty += 1          # 19.10: Gemma иногда заканчивает одним «размышлением» без текста — попросить ответ
            tr(turn, "empty", f"пустой ответ ({r.get('eval_count') or 0} ток.) — следующий ход без размышления")
            msgs.append({"role": "user", "content": "Your reply was empty. Reply now with ONLY the final JSON object described in SKILL.md."})
            continue
        if not m.get("tool_calls") and fixes < 2 and (err := (bad_veto(m.get("content")) if veto
                                                              else bad_answer(m.get("content"), labels))):
            fixes += 1          # 19.10 g1: ответ не по форме SKILL.md — вернуть с причиной
            tr(turn, "fix", err)
            msgs.append({"role": "user", "content": f"Your answer is not valid: {err}. Reply now with ONLY the final JSON object "
                                                     + ("described in SKILL.md (p_win, cancel, why). Make it agree with your own analysis."
                                                        if veto else
                                                        "described in SKILL.md (max_f, probs with every bucket label exactly as in "
                                                        "the letter, summing to 1, why). Make probs agree with your own analysis.")})
            continue
        if not m.get("tool_calls"):
            tr(turn, "final", f"ответ готов: ходов {turn}, python {calls}, токенов {tokens}")
            print(json.dumps({"status": "SUCCESS", "response": m.get("content", ""), "turns": turn, "tokens": tokens, "calls": calls,
                              "empty": empty, "fixes": fixes,
                              **({"cost": round(cost, 4), "providers": sorted(providers)} if BACKEND == "openrouter" else {})},
                             ensure_ascii=False))
            return
        for tc in m["tool_calls"]:
            calls += 1
            cmd = (tc.get("function", {}).get("arguments") or {}).get("command", "")
            tr(turn, "call", cmd or "(пустая команда)")
            out = sh(cmd, work) if cmd else "(empty command)"
            tr(turn, "result", out)
            msgs.append({"role": "tool", "tool_call_id": tc.get("id"), "content": out} if BACKEND == "openrouter"
                        else {"role": "tool", "tool_name": "bash", "content": out})
        if deadline and time.time() > deadline:
            msgs.append({"role": "user", "content": "Time is up. Reply now with ONLY the final JSON object, no more commands."})
            deadline = None
    tr(MAX_TURNS, "error", "too many turns")
    print(json.dumps({"status": "ERROR", "error": "too many turns", "turns": MAX_TURNS, "tokens": tokens, "calls": calls,
                      "cost": round(cost, 4)}))


if __name__ == "__main__":
    main()
