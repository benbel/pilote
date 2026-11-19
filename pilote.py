import base64
import ctypes
import datetime
import getpass
import gzip
import html
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Input, Static

DATALAB = "https://datalab.sspcloud.fr"
TOKEN_URL = "https://auth.lab.sspcloud.fr/auth/realms/sspcloud/protocol/openid-connect/token"
CLIENT_ID = "onyxia"
K8S = "https://apiserver.kub.sspcloud.fr"
INIT_URL = os.environ.get(
    "PILOTE_INIT_URL",
    "https://raw.githubusercontent.com/benbel/pilote/main/init.sh",
    )
HOME = os.path.expanduser("~")
RT_FILE = os.path.join(HOME, ".krt")
REFRESH_SECONDS = 10
WAIT_SECONDS = 900
PLURALS = {"Ingress": "ingresses", "NetworkPolicy": "networkpolicies"}
WORKLOADS = ("StatefulSet", "Deployment")
BROKEN = ("ImagePullBackOff", "ErrImagePull", "InvalidImageName", "CrashLoopBackOff")


class AuthError(Exception):
    pass


class ApiError(Exception):
    pass


def http(method, url, token=None, data=None, form=None):
    headers = {"Accept": "application/json"}
    body = None
    if token:
        headers["Authorization"] = "Bearer " + token
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, parse_json(response.read())
    except urllib.error.HTTPError as error:
        return error.code, parse_json(error.read())


def parse_json(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def jwt_claims(token):
    part = token.split(".")[1]
    padding = "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part + padding))


def read_refresh_token():
    if not os.path.exists(RT_FILE):
        return None
    with open(RT_FILE) as f:
        value = f.read().strip()
    if not value:
        return None
    return value


def save_refresh_token(value):
    tmp = RT_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(value)
    os.replace(tmp, RT_FILE)


class Auth:
    def __init__(self):
        self.refresh_token = read_refresh_token()
        self.id_token = None
        self.expires_at = 0
        self.claims = {}
        self.lock = threading.Lock()

    def token(self):
        with self.lock:
            if self.id_token is None or time.time() > self.expires_at - 30:
                self.refresh()
            return self.id_token

    def refresh(self):
        if not self.refresh_token:
            raise AuthError("aucun refresh-token")
        code, answer = http(
            "POST",
            TOKEN_URL,
            form={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": self.refresh_token,
                },
            )
        if code != 200 or "id_token" not in answer:
            raise AuthError(answer.get("error_description", "HTTP %s" % code))
        if answer.get("refresh_token"):
            self.refresh_token = answer["refresh_token"]
            save_refresh_token(self.refresh_token)
        self.id_token = answer["id_token"]
        self.claims = jwt_claims(self.id_token)
        self.expires_at = self.claims["exp"]

    def namespace(self):
        return "user-" + self.claims["preferred_username"]


def ask_refresh_token(reason):
    if reason:
        print("Jeton refusé : %s" % reason)
    value = getpass.getpass("refresh-token (Mon compte → Kubernetes) : ")
    value = re.sub(r"\s+", "", value)
    if not value:
        sys.exit("Aucun jeton.")
    save_refresh_token(value)


def login():
    reason = None
    while True:
        auth = Auth()
        if not auth.refresh_token:
            ask_refresh_token(reason)
            continue
        try:
            auth.token()
            return auth
        except AuthError as error:
            reason = str(error)
            os.remove(RT_FILE)
        except urllib.error.URLError as error:
            sys.exit("Impossible de joindre %s : %s" % (TOKEN_URL, error.reason))


def plural(kind):
    if kind in PLURALS:
        return PLURALS[kind]
    return kind.lower() + "s"


def parse_manifest(manifest):
    resources = []
    for doc in re.split(r"(?m)^---\s*$", manifest):
        api = re.search(r"(?m)^apiVersion:\s*(\S+)", doc)
        kind = re.search(r"(?m)^kind:\s*(\S+)", doc)
        meta = re.search(r"(?ms)^metadata:\s*\n(.*?)(?=^\S|\Z)", doc)
        if not (api and kind and meta):
            continue
        name = re.search(r"(?m)^  name:\s*(.+?)\s*$", meta.group(1))
        if not name:
            continue
        resources.append((api.group(1), kind.group(1), name.group(1).strip("'\"")))
    return resources


def resource_path(namespace, api_version, kind, name):
    base = "/apis/" + api_version
    if api_version == "v1":
        base = "/api/v1"
    return "%s/namespaces/%s/%s/%s" % (base, namespace, plural(kind), urllib.parse.quote(name))


def decode_release(secret):
    raw = base64.b64decode(secret["data"]["release"])
    raw = base64.b64decode(raw)
    try:
        return json.loads(gzip.decompress(raw))
    except (OSError, ValueError):
        return None


def parse_time(value):
    match = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)?", value or "")
    if not match:
        return None
    moment = (
        datetime.datetime
        .strptime(match.group(1), "%Y-%m-%dT%H:%M:%S")
        .replace(tzinfo=datetime.timezone.utc)
        )
    offset = match.group(2)
    if offset and offset != "Z":
        delta = datetime.timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6]))
        if offset[0] == "+":
            moment = moment - delta
        else:
            moment = moment + delta
    return moment.timestamp()


def human_age(seconds):
    if seconds is None:
        return "?"
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%d s" % seconds
    if seconds < 3600:
        return "%d min" % (seconds // 60)
    if seconds < 86400:
        return "%d h" % (seconds // 3600)
    return "%d j" % (seconds // 86400)


def short_repo(url):
    if not url:
        return "—"
    url = re.sub(r"^https?://(www\.)?github\.com/", "", url)
    return re.sub(r"\.git$", "", url)


def pods_of(pods, workloads):
    found = []
    for pod in pods:
        for workload in workloads:
            if pod["metadata"]["name"].startswith(workload + "-"):
                found.append(pod)
    return found


def pod_state(release_status, pods, suspended):
    if release_status == "failed":
        return "Erreur"
    if not pods and suspended:
        return "Suspendu"
    if release_status not in (None, "deployed") or not pods:
        return "Démarrage"
    status = pods[0].get("status", {})
    containers = status.get("initContainerStatuses", []) + status.get("containerStatuses", [])
    for container in containers:
        if container.get("state", {}).get("waiting", {}).get("reason") in BROKEN:
            return "Erreur"
    phase = status.get("phase")
    if phase == "Pending":
        return "Démarrage"
    if phase != "Running":
        return "Erreur"
    ready = status.get("containerStatuses", [])
    for container in ready:
        if not container.get("ready"):
            return "Démarrage"
    if not ready:
        return "Démarrage"
    return "Prêt"


def section(values, key):
    return values.get(key) or {}


class Kube:
    def __init__(self, auth):
        self.auth = auth
        self.namespace = auth.namespace()

    def call(self, method, path, data=None):
        code, answer = http(method, K8S + path, self.auth.token(), data=data)
        if code == 401:
            raise AuthError("jeton refusé par Kubernetes")
        return code, answer

    def latest_releases(self):
        code, answer = self.call(
            "GET",
            "/api/v1/namespaces/%s/secrets?labelSelector=owner%%3Dhelm" % self.namespace,
            )
        if code != 200:
            raise ApiError("lecture des releases : HTTP %s" % code)
        latest = {}
        versions = {}
        for secret in answer.get("items", []):
            labels = secret["metadata"].get("labels", {})
            name = labels.get("name")
            version = int(labels.get("version", 0))
            if name and version > versions.get(name, -1):
                versions[name] = version
                latest[name] = secret
        return latest

    def pods(self):
        code, answer = self.call("GET", "/api/v1/namespaces/%s/pods" % self.namespace)
        if code != 200:
            return []
        return answer.get("items", [])

    def services(self):
        releases = self.latest_releases()
        pods = self.pods()
        now = time.time()
        rows = []
        for name, secret in releases.items():
            release = decode_release(secret)
            if release is None:
                continue
            config = release.get("config") or {}
            info = release.get("info") or {}
            resources = parse_manifest(release.get("manifest", ""))
            workloads = []
            for api, kind, resource in resources:
                if kind in WORKLOADS:
                    workloads.append(resource)
            first = parse_time(info.get("first_deployed"))
            age = None
            if first:
                age = now - first
            rows.append(
                {
                    "name": name,
                    "repo": short_repo(section(config, "git").get("repository")),
                    "state": pod_state(
                        info.get("status"),
                        pods_of(pods, workloads),
                        bool(section(config, "global").get("suspend")),
                        ),
                    "age": human_age(age),
                    "first": first or 0,
                    "host": section(config, "ingress").get("hostname"),
                    "password": section(config, "security").get("password"),
                    "resources": resources,
                    }
                )
        rows.sort(key=row_first, reverse=True)
        return rows

    def delete(self, row):
        errors = []
        options = {"kind": "DeleteOptions", "apiVersion": "v1", "propagationPolicy": "Background"}
        for api, kind, name in reversed(row["resources"]):
            code, answer = self.call("DELETE", resource_path(self.namespace, api, kind, name), options)
            if code not in (200, 202, 404):
                errors.append("%s/%s : HTTP %s" % (kind, name, code))
        selector = urllib.parse.quote("owner=helm,name=%s" % row["name"])
        code, answer = self.call(
            "GET",
            "/api/v1/namespaces/%s/secrets?labelSelector=%s" % (self.namespace, selector),
            )
        secrets = ["sh.onyxia.release.v1." + row["name"]]
        for secret in answer.get("items", []):
            secrets.append(secret["metadata"]["name"])
        for secret in secrets:
            code, answer = self.call(
                "DELETE",
                "/api/v1/namespaces/%s/secrets/%s" % (self.namespace, secret),
                options,
                )
            if code not in (200, 202, 404):
                errors.append("secret %s : HTTP %s" % (secret, code))
        if errors:
            raise ApiError("; ".join(errors))


def row_first(row):
    return row["first"]


def parse_repo(text):
    text = text.strip()
    text = re.sub(r"^(https?://)?(www\.)?github\.com/", "", text)
    text = re.sub(r"(\.git)?/?$", "", text)
    match = re.fullmatch(r"([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)", text)
    if not match:
        return None
    return match.group(1), match.group(2)


def onyxia_value(value):
    return urllib.parse.quote("«%s»" % value, safe="")


def launcher_url(owner, repo):
    name = re.sub(r"[^a-z0-9-]+", "-", repo.lower()).strip("-")[:30]
    params = [
        "autoLaunch=true",
        "name=" + urllib.parse.quote(name or "vscode", safe=""),
        "git.repository=" + onyxia_value("https://github.com/%s/%s.git" % (owner, repo)),
        "git.asCodeServerRoot=true",
        "init.personalInit=" + onyxia_value(INIT_URL),
        ]
    return DATALAB + "/launcher/ide/vscode-python?" + "&".join(params)


def find_firefox():
    roots = [
        os.environ.get("SCOOP", ""),
        os.path.join(HOME, "scoop"),
        os.environ.get("SCOOP_GLOBAL", ""),
        os.path.join(os.environ.get("ProgramData", ""), "scoop"),
        ]
    for root in roots:
        path = os.path.join(root, "apps", "firefox", "current", "firefox.exe")
        if root and os.path.isfile(path):
            return path
    return None


def open_url(url):
    subprocess.Popen([find_firefox(), url])


def remove_file(path):
    if os.path.exists(path):
        os.remove(path)


def open_logged_in(host, password):
    host = html.escape(host)
    password = html.escape(password)
    page = (
        '<!doctype html><meta charset="utf-8">'
        f'<form method="post" action="https://{host}/login?to=%2F">'
        '<input type="hidden" name="base" value=".">'
        f'<input type="hidden" name="href" value="https://{host}/login">'
        f'<input type="hidden" name="password" value="{password}"></form>'
        "<script>document.forms[0].submit()</script>"
        )
    handle, path = tempfile.mkstemp(suffix=".html")
    with os.fdopen(handle, "w", encoding="utf-8") as f:
        f.write(page)
    open_url(pathlib.Path(path).as_uri())
    threading.Timer(30, remove_file, args=[path]).start()


def foreground_window():
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    return user32.GetForegroundWindow()


def grab_foreground(hwnd, seconds):
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
    user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int]
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(0.25)
        current = user32.GetForegroundWindow()
        if not current or current == hwnd:
            continue
        me = kernel32.GetCurrentThreadId()
        other = user32.GetWindowThreadProcessId(current, None)
        user32.AttachThreadInput(me, other, True)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        user32.AttachThreadInput(me, other, False)
        if user32.GetForegroundWindow() != hwnd:
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            user32.SetForegroundWindow(hwnd)


def keep_foreground(hwnd):
    if not hwnd:
        return
    threading.Thread(target=grab_foreground, args=[hwnd, 1], daemon=True).start()


def copy_to_clipboard(text):
    try:
        subprocess.run(["clip"], input=text.encode(), check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


class RepoInput(Input):
    BINDINGS = [
        Binding("down", "app.focus_table", "liste", show=False),
        ]


class Pilote(App):
    TITLE = "pilote"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
        Screen, #page, Static { background: ansi_default; color: ansi_default; }
        #page { max-width: 90; padding: 2 2; }
        #title { margin: 0 0 1 2; }
        #saisie { height: 1; margin: 0 0 1 2; }
        #saisie Static { width: auto; }
        Input, Input:focus { background: ansi_default; background-tint: transparent; }
        DataTable, DataTable:focus { height: 1fr; background: ansi_default; background-tint: transparent; }
        DataTable > .datatable--header, DataTable:focus > .datatable--header { background: ansi_default; background-tint: transparent; text-style: none; }
        DataTable > .datatable--cursor, DataTable:focus > .datatable--cursor { background: ansi_default; color: ansi_default; text-style: none; }
        DataTable > .datatable--hover { background: ansi_default; }
        #status { height: 1; margin-left: 2; }
        Footer { background: ansi_default; padding: 0 3; }
        FooterKey { background: ansi_default; }
        FooterKey .footer-key--key { background: ansi_default; color: ansi_default; text-style: bold; }
        FooterKey .footer-key--description { color: ansi_default; }
        """
    BINDINGS = [
        Binding("o", "open", "ouvrir"),
        Binding("s", "delete", "supprimer"),
        Binding("q", "quit", "quitter"),
        Binding("ctrl+c", "quit", "quitter", show=False, priority=True),
        Binding("escape", "focus_input", "saisie", show=False),
        ]

    def __init__(self, kube, terminal=None):
        super().__init__(ansi_color=True)
        self.kube = kube
        self.terminal = terminal
        self.rows = []
        self.waiting = []
        self.confirming = None
        self.busy = threading.Lock()

    def compose(self) -> ComposeResult:
        with Vertical(id="page"):
            yield Static("pilote", id="title")
            with Horizontal(id="saisie"):
                yield Static("Dépôt : ")
                yield RepoInput(compact=True, id="repo")
            yield DataTable(cursor_type="row", cell_padding=0, id="table")
            yield Static("", id="status")
        yield Footer()

    def on_mount(self):
        self.theme = "ansi-dark"
        table = self.query_one(DataTable)
        table.add_column("  ", key="mark", width=2)
        table.add_column("Nom   ", key="name")
        table.add_column("Dépôt   ", key="repo")
        table.add_column("État   ", key="state")
        table.add_column("Âge", key="age")
        self.query_one(RepoInput).focus()
        self.action_refresh()
        self.set_interval(REFRESH_SECONDS, self.action_refresh)

    def status(self, text):
        self.query_one("#status", Static).update(text)

    def selected(self):
        table = self.query_one(DataTable)
        if table.cursor_row is None or table.cursor_row >= len(self.rows):
            return None
        return self.rows[table.cursor_row]

    def check_action(self, action, parameters):
        if action in ("open", "delete", "quit") and isinstance(self.focused, Input):
            return False
        return True

    @work(thread=True, exclusive=True, group="refresh")
    def action_refresh(self):
        if not self.busy.acquire(blocking=False):
            return
        try:
            rows = self.kube.services()
            self.call_from_thread(self.show_rows, rows)
        except AuthError:
            self.call_from_thread(self.exit, "Jeton expiré : relancer pilote.")
        except Exception as error:
            self.call_from_thread(self.status, "⚠ %s" % error)
        finally:
            self.busy.release()

    def show_rows(self, rows):
        table = self.query_one(DataTable)
        current = self.selected()
        self.rows = rows
        table.clear()
        for row in rows:
            table.add_row(
                "",
                row["name"] + "   ",
                row["repo"] + "   ",
                row["state"] + "   ",
                row["age"],
                key=row["name"],
                )
        for index, row in enumerate(rows):
            if current and row["name"] == current["name"]:
                table.move_cursor(row=index)
        self.mark()
        self.open_new_ready(rows)

    def open_new_ready(self, rows):
        now = time.time()
        waiting = []
        for launch in self.waiting:
            if now - launch["since"] < WAIT_SECONDS:
                waiting.append(launch)
        self.waiting = waiting
        if not self.waiting:
            return
        known = self.waiting[0]["known"]
        for row in rows:
            if row["name"] not in known and row["state"] == "Prêt":
                self.waiting.pop(0)
                for launch in self.waiting:
                    launch["known"].add(row["name"])
                self.open_row(row)
                return

    def mark(self):
        table = self.query_one(DataTable)
        in_list = not isinstance(self.focused, Input)
        for index, row in enumerate(self.rows):
            sign = ""
            if in_list and index == table.cursor_row:
                sign = "›"
            table.update_cell(row["name"], "mark", sign)

    def on_data_table_row_highlighted(self, event):
        self.mark()

    def on_descendant_focus(self, event):
        self.mark()

    def on_descendant_blur(self, event):
        self.mark()

    def action_focus_table(self):
        self.query_one(DataTable).focus()

    def action_focus_input(self):
        self.query_one(RepoInput).focus()

    def on_input_submitted(self, event):
        parsed = parse_repo(event.value)
        if not parsed:
            self.status("Format : propriétaire/dépôt")
            return
        known = set()
        for row in self.rows:
            known.add(row["name"])
        open_url(launcher_url(parsed[0], parsed[1]))
        keep_foreground(self.terminal)
        self.waiting.append({"since": time.time(), "known": known})
        event.input.value = ""
        self.status("Lancement…")

    def action_open(self):
        row = self.selected()
        if row:
            self.open_row(row)

    def open_row(self, row):
        if not row["host"]:
            return
        if not row["password"]:
            open_url("https://" + row["host"])
            return
        open_logged_in(row["host"], row["password"])
        if copy_to_clipboard(row["password"]):
            self.status("Mot de passe copié")
        else:
            self.status("Copie du mot de passe impossible")

    def action_delete(self):
        row = self.selected()
        if not row:
            return
        self.confirming = row
        self.status("Supprimer %s ? o/n" % row["name"])

    def on_key(self, event):
        if self.confirming is None:
            return
        row = self.confirming
        self.confirming = None
        event.stop()
        event.prevent_default()
        if event.key != "o":
            self.status("")
            return
        self.status("Suppression…")
        self.do_delete(row)

    @work(thread=True)
    def do_delete(self, row):
        try:
            self.kube.delete(row)
            self.call_from_thread(self.status, "Supprimé")
        except AuthError:
            self.call_from_thread(self.exit, "Jeton expiré : relancer pilote.")
        except Exception as error:
            self.call_from_thread(self.status, "⚠ %s" % error)
        self.call_from_thread(self.action_refresh)


def main():
    if not find_firefox():
        sys.exit("Firefox de Scoop introuvable (scoop install firefox).")
    terminal = foreground_window()
    kube = Kube(login())
    message = Pilote(kube, terminal).run()
    if message:
        print(message)


if __name__ == "__main__":
    main()
