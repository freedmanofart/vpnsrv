#!/usr/bin/env python3
import getpass
import json
import shlex
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

EXPECTED_USERNAME = "FreedomVPNSupportAI_bot"
REMOTE = r'''import json, os, stat, subprocess, sys, tempfile, time
from pathlib import Path
value=sys.stdin.buffer.readline().decode().rstrip("\r\n")
if not value or "\n" in value or "\r" in value:
    print("token_input_invalid"); raise SystemExit(2)
p=Path("/opt/openclaw-freedomvpn/.env")
lines=p.read_text().splitlines()
indices=[i for i,line in enumerate(lines) if line.startswith("TELEGRAM_BOT_TOKEN=")]
if len(indices)!=1:
    print("env_entry_count_invalid"); raise SystemExit(3)
st=p.stat()
lines[indices[0]]="TELEGRAM_BOT_TOKEN="+value
fd,tmp=tempfile.mkstemp(prefix=".env.",dir=str(p.parent),text=True)
try:
    with os.fdopen(fd,"w") as f:
        f.write("\n".join(lines)+"\n"); f.flush(); os.fsync(f.fileno())
    os.chmod(tmp,stat.S_IMODE(st.st_mode)); os.chown(tmp,st.st_uid,st.st_gid); os.replace(tmp,p)
finally:
    if os.path.exists(tmp): os.unlink(tmp)
cmd=["docker","compose","up","-d","--force-recreate","openclaw-gateway"]
r=subprocess.run(cmd,cwd="/opt/openclaw-freedomvpn",capture_output=True,text=True,timeout=90)
if r.returncode:
    print("compose_recreate_failed"); raise SystemExit(4)
health="starting"
for _ in range(60):
    q=subprocess.run(["docker","inspect","-f","{{.State.Health.Status}}","openclaw-freedomvpn"],capture_output=True,text=True,timeout=10)
    health=q.stdout.strip() or "unknown"
    if health in ("healthy","unhealthy"): break
    time.sleep(2)
probe=subprocess.run(["docker","exec","openclaw-freedomvpn","openclaw","channels","status","--probe"],capture_output=True,text=True,timeout=60)
text=(probe.stdout+probe.stderr).lower()
if "telegram default:" in text and ("connected" in text or "works" in text) and "error:" not in text:
    telegram="connected"
elif "unauthorized" in text or "probe failed" in text:
    telegram="error"
else:
    telegram="unknown"
print("token_saved=true container_health="+health+" telegram="+telegram)
'''

token = getpass.getpass("Введите активный токен BotFather для @FreedomVPNSupportAI_bot (ввод скрыт): ").strip()
if not token:
    print("Пустой ввод; сервер не менялся.")
    raise SystemExit(1)
try:
    with urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=15) as response:
        payload = json.load(response)
except (HTTPError, URLError, TimeoutError):
    print("Telegram не подтвердил токен; сервер не менялся.")
    raise SystemExit(1)
if not payload.get("ok") or payload.get("result", {}).get("username") != EXPECTED_USERNAME:
    print("Токен не принадлежит ожидаемому боту; сервер не менялся.")
    raise SystemExit(1)
print("Токен принят Telegram для ожидаемого бота. Обновляю серверную конфигурацию.")
try:
    remote_command = "python3 -c " + shlex.quote(REMOTE)
    result = subprocess.run(
        ["ssh", "root@freedomvpn", remote_command],
        input=token + "\n", capture_output=True, text=True, timeout=300,
    )
except (subprocess.SubprocessError, OSError):
    print("Не удалось завершить удалённое обновление; проверьте SSH и состояние сервера.")
    raise SystemExit(2)
if result.returncode:
    print("Сервер не подтвердил обновление (код "+str(result.returncode)+").")
    if result.stdout.strip(): print(result.stdout.strip())
    if result.stderr.strip(): print(result.stderr.strip())
    raise SystemExit(result.returncode)
print(result.stdout.strip() or "Обновление завершено.")
