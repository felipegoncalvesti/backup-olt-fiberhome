#!/usr/bin/env python3
"""
Backup da OLT Fiberhome AN5516-06 (RP1400) via telnet.

Gera, a cada execução, dois arquivos no diretório de saída:
  1) olt1-config-AAAA-MM-DD_HHMM.txt   -> 'show startup-config' completo (bloco de notas)
  2) olt1-mapa-onu-vlan-AAAA-MM-DD_HHMM.xlsx -> MAC, placa, PON, ONU ID, modelo, VLAN

Mantém também as cópias "olt1-config-latest.txt" e "olt1-mapa-onu-vlan-latest.xlsx"
(sempre a última execução válida) e apaga arquivos com mais de RETENTION_DAYS dias.

Dependências: Python 3.8+ e openpyxl (sem ele gera CSV). paramiko só se usar o envio por SFTP.
Não usa telnetlib (removido no Python 3.13).

Configuração: arquivo .conf (um por OLT) passado como argumento (veja olt.conf.example).

Exemplo de cron (todo dia às 02:00):
  0 2 * * * /usr/bin/python3 /opt/olt-fiberhome/olt_backup.py /opt/olt-fiberhome/olt1.conf >> /var/log/olt-fiberhome.log 2>&1
"""
import csv
import os
import re
import socket
import sys
import time
from datetime import datetime, timedelta

# ----------------------------------------------------------------- configuração
# Um arquivo .conf por OLT (veja olt.conf.example). Uso:  olt_backup.py [/caminho/olt1.conf]
import configparser

CONF_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "olt1.conf")
_cp = configparser.ConfigParser(interpolation=None)
if not _cp.read(CONF_PATH, encoding="utf-8"):
    sys.exit(f"arquivo de configuração não encontrado: {CONF_PATH}")
_o, _s, _l = _cp["olt"], (_cp["sftp"] if _cp.has_section("sftp") else {}), _cp["local"]

HOST = _o["host"]
PORT = int(_o.get("port", "23"))
USER = _o["user"]
PASSWORD = _o["password"]
ENABLE_USER = _o.get("enable_user", "en")
ENABLE_PASS = _o["enable_pass"]
NAME = _o.get("name", "olt1")                                   # prefixo dos arquivos
SLOTS = [x.strip() for x in _o["slots"].split(",") if x.strip()]
OUT_DIR = _l.get("out_dir", "/var/backups/olt-fiberhome")
RETENTION_DAYS = int(_l.get("retention_days", "90"))            # 0 = nunca apagar
CMD_TIMEOUT = int(_l.get("cmd_timeout", "900"))
SFTP_ON = _s.get("enabled", "no").lower() in ("1", "yes", "true", "sim")

PAGER = "Press any key to continue"
PROMPT_RE = re.compile(r"[\w\\./-]+#\s*$")      # ex.: OLT1#  /  OLT1\onu\lan#
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PAGER_RE = re.compile(r"[ \x00]*--Press any key to continue Ctrl\+c to stop--[ \x00\x08]*")


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ----------------------------------------------------------------- telnet cru
class Olt:
    def __init__(self):
        self.s = socket.create_connection((HOST, PORT), timeout=15)
        self.s.settimeout(1.0)

    def _recv(self, wait):
        """Lê o que chegar em 'wait' segundos, recusando negociações IAC."""
        data = b""
        end = time.time() + wait
        while time.time() < end:
            try:
                d = self.s.recv(8192)
            except socket.timeout:
                if data:
                    break
                continue
            if not d:
                raise ConnectionError("conexão fechada pela OLT")
            i = 0
            while i < len(d):
                if d[i] == 255 and i + 2 < len(d):
                    cmd, opt = d[i + 1], d[i + 2]
                    if cmd in (253, 254):
                        self.s.send(bytes([255, 252, opt]))
                    elif cmd in (251, 252):
                        self.s.send(bytes([255, 254, opt]))
                    i += 3
                else:
                    data += d[i:i + 1]
                    i += 1
        return data.decode("latin1")

    def login(self):
        self._recv(3)
        for x in (USER, PASSWORD, ENABLE_USER, ENABLE_PASS):
            self.s.send((x + "\r\n").encode())
            self._recv(2)
        self.s.send(b"\r\n")
        out = self._recv(2)
        if not PROMPT_RE.search(out):
            raise RuntimeError("login falhou (prompt não apareceu): %r" % out[-200:])

    def _chunk(self, wait=0.4):
        """Uma leitura só (sem esperar silêncio): devolve '' se nada chegou em 'wait' s."""
        self.s.settimeout(wait)
        try:
            d = self.s.recv(65536)
        except socket.timeout:
            return ""
        finally:
            self.s.settimeout(1.0)
        if not d:
            raise ConnectionError("conexão fechada pela OLT")
        out = bytearray()
        i = 0
        while i < len(d):
            if d[i] == 255 and i + 2 < len(d):
                cmd, opt = d[i + 1], d[i + 2]
                if cmd in (253, 254):
                    self.s.send(bytes([255, 252, opt]))
                elif cmd in (251, 252):
                    self.s.send(bytes([255, 254, opt]))
                i += 3
            else:
                out.append(d[i])
                i += 1
        return out.decode("latin1")

    def run(self, command, timeout=CMD_TIMEOUT):
        """Executa o comando, avança o paginador assim que ele aparece e devolve o texto limpo."""
        self.s.send((command + "\r\n").encode())
        parts = []
        tail = ""
        t0 = time.time()
        while time.time() - t0 < timeout:
            d = self._chunk()
            if d:
                parts.append(d)
                tail = (tail + d)[-400:]
                if PAGER in tail[-200:]:
                    self.s.send(b" ")
                    tail = tail.replace(PAGER, "")
                continue
            if PROMPT_RE.search(tail):   # silêncio + prompt = comando terminou
                break
        else:
            raise TimeoutError(f"timeout executando: {command}")
        t = ANSI_RE.sub("", "".join(parts))
        t = PAGER_RE.sub("\n", t)
        return t.replace("\x08", "").replace("\x00", "").replace("\r", "")

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


# ----------------------------------------------------------------- parsers
AUTH_RE = re.compile(
    r"^set autho sl (\d+) p (\d+) ty (\S+) o (\d+) phy (\S+) pas", re.M)
ROW_RE = re.compile(
    r"^\s*\d+\s+(\d+)/(\d+)\s*/(\d+)\s+(\d+)\s+(\d+)\s+(\w+)\s+(\w+)\s+(\S+)\s", re.M)


def parse_auth(config_text):
    """(slot, pon, onu) -> (mac/id físico, modelo)"""
    return {(int(a), int(b), int(d)): (e, c)
            for a, b, c, d, e in AUTH_RE.findall(config_text)}


def parse_vlans(text):
    """linhas de 'show onufe_service' -> (slot, pon, onu, porta, modo, vlan)"""
    rows = []
    for slot, pon, onu, port, _svc, _type, mode, cvid in ROW_RE.findall(text):
        rows.append((int(slot), int(pon), int(onu), int(port), mode, cvid))
    return rows


# ----------------------------------------------------------------- saídas
HEADER = ["MAC/ID fisico", "Placa", "PON", "ONU ID", "Modelo", "Porta", "Modo VLAN", "VLAN (CVID)"]


def write_xlsx_or_csv(path_base, table):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError:
        path = path_base + ".csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(HEADER)
            w.writerows(table)
        log("AVISO: openpyxl não instalado (pip install openpyxl) - gerado CSV em vez de XLSX")
        return path
    wb = Workbook()
    ws = wb.active
    ws.title = "ONU x VLAN"
    ws.append(HEADER)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in table:
        row = list(r)
        if str(row[7]).isdigit():
            row[7] = int(row[7])
        ws.append(row)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col, w in zip("ABCDEFGH", (18, 8, 6, 8, 14, 7, 11, 12)):
        ws.column_dimensions[col].width = w
    path = path_base + ".xlsx"
    wb.save(path)
    return path


def replace_file(src, dst):
    if os.path.exists(dst):
        os.remove(dst)
    # cópia (não symlink) para funcionar em qualquer file server/frontend
    with open(src, "rb") as a, open(dst, "wb") as b:
        b.write(a.read())


def cleanup(out_dir):
    if RETENTION_DAYS <= 0:
        return
    limit = time.time() - RETENTION_DAYS * 86400
    for f in os.listdir(out_dir):
        if f.startswith(NAME + "-") and "latest" not in f:
            p = os.path.join(out_dir, f)
            if os.path.isfile(p) and os.path.getmtime(p) < limit:
                os.remove(p)
                log(f"removido (retenção {RETENTION_DAYS}d): {f}")



def sftp_upload_once(files):
    """Envia os arquivos ao servidor de arquivos por SFTP e aplica retenção lá também."""
    import paramiko
    t = paramiko.Transport((_s["host"], int(_s.get("port", "22"))))
    t.connect(username=_s["user"], password=_s["password"])
    c = paramiko.SFTPClient.from_transport(t)
    try:
        rdir = _s.get("remote_dir", "/")
        for local, remote_name in files:
            c.put(local, rdir.rstrip("/") + "/" + remote_name)
            log(f"SFTP ok -> {rdir.rstrip('/')}/{remote_name}")
        if RETENTION_DAYS > 0:
            limit = time.time() - RETENTION_DAYS * 86400
            for a in c.listdir_attr(rdir):
                if a.filename.startswith(NAME + "-") and "latest" not in a.filename and a.st_mtime < limit:
                    c.remove(rdir.rstrip("/") + "/" + a.filename)
                    log(f"SFTP removido (retenção): {a.filename}")
    finally:
        c.close()
        t.close()

def sftp_upload(files, tries=4):
    for n in range(1, tries + 1):
        try:
            return sftp_upload_once(files)
        except Exception as e:
            log(f"SFTP falhou ({n}/{tries}): {type(e).__name__}: {e}")
            if n == tries:
                raise
            time.sleep(15)


# ----------------------------------------------------------------- principal
def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    olt = Olt()
    try:
        olt.login()
        log("login ok")

        log("coletando show startup-config (pode levar alguns minutos)...")
        config = olt.run("show startup-config")
        if not re.search(r"!end\s+of config", config):
            with open(os.path.join(OUT_DIR, f"{NAME}-config-INCOMPLETO.txt"), "w", encoding="utf-8") as f:
                f.write(config)
            raise RuntimeError("startup-config incompleto (sem '!end of config'); salvo como -INCOMPLETO.txt para análise")
        log(f"startup-config: {len(config.splitlines())} linhas")

        olt.run("cd onu")
        olt.run("cd lan")
        vlans = []
        for slot in SLOTS:
            log(f"coletando VLAN das ONUs do slot {slot}...")
            txt = olt.run(f"show onufe_service slot {slot} pon all onu all")
            part = parse_vlans(txt)
            log(f"slot {slot}: {len(part)} ONUs com serviço")
            vlans += part
        if not vlans:
            raise RuntimeError("nenhuma ONU/VLAN lida; nada foi gravado")
    finally:
        olt.close()

    auth = parse_auth(config)
    table = []
    for slot, pon, onu, port, mode, cvid in sorted(vlans):
        mac, model = auth.get((slot, pon, onu), ("", ""))
        table.append([mac, slot, pon, onu, model, port, mode, cvid])

    # --- arquivo 1: configuração em txt
    cfg_path = os.path.join(OUT_DIR, f"{NAME}-config-{stamp}.txt")
    with open(cfg_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(config)
    # --- arquivo 2: mapa ONU x VLAN em Excel
    map_path = write_xlsx_or_csv(os.path.join(OUT_DIR, f"{NAME}-mapa-onu-vlan-{stamp}"), table)

    # cópias "latest" para o frontend apontar sempre para o mesmo nome
    replace_file(cfg_path, os.path.join(OUT_DIR, f"{NAME}-config-latest.txt"))
    ext = os.path.splitext(map_path)[1]
    replace_file(map_path, os.path.join(OUT_DIR, f"{NAME}-mapa-onu-vlan-latest{ext}"))

    cleanup(OUT_DIR)
    if SFTP_ON:
        base = os.path.basename
        sftp_upload([(cfg_path, base(cfg_path)), (map_path, base(map_path)),
                     (os.path.join(OUT_DIR, f"{NAME}-config-latest.txt"), f"{NAME}-config-latest.txt"),
                     (os.path.join(OUT_DIR, f"{NAME}-mapa-onu-vlan-latest{ext}"), f"{NAME}-mapa-onu-vlan-latest{ext}")])
    log(f"OK -> {cfg_path}")
    log(f"OK -> {map_path} ({len(table)} ONUs)")


if __name__ == "__main__":
    attempts = int(os.environ.get("RETRIES", "3"))
    pause = int(os.environ.get("RETRY_PAUSE", "120"))
    for n in range(1, attempts + 1):
        try:
            main()
            sys.exit(0)
        except Exception as e:
            log(f"ERRO (tentativa {n}/{attempts}): {type(e).__name__}: {e}")
            if n < attempts:
                time.sleep(pause)
    sys.exit(1)  # cron: código != 0 quando todas as tentativas falham
