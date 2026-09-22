"""Archive automatique des preuves Technocore (messages signés et preuves de contribution).

S'appuie sur technocore_agent.py (technocore-did-starter) pour tout ce qui touche à la clé
privée : la passphrase est saisie dans ce script-là, jamais ici. Cet outil capture sa sortie,
vérifie les signatures hors ligne, confirme auprès du serveur, horodate avec OpenTimestamps et
régénère un rapport en JSON, CSV, Markdown et HTML.

Commandes :
  say ROOM TEXTE        publie un message signé puis l'archive
  proof URL COMMIT      signe une contribution Git puis l'archive
  importer [FICHIER…]   archive des sorties déjà obtenues (fichiers, ou presse-papiers si vide)
  horodater             envoie les preuves non horodatées à OpenTimestamps
  completer             récupère les attestations Bitcoin des horodatages en attente
  verifier              revérifie toutes les signatures archivées
  rapport               régénère les rapports
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

BASE_URL = os.environ.get("TECHNOCORE_URL", "https://technocore.chat").rstrip("/")
DEFAULT_AGENT_DIR = Path(os.environ.get("TECHNOCORE_AGENT_DIR", "~/Desktop/claude/flop-did"))
DEFAULT_ARCHIVE = Path(os.environ.get("TECHNOCORE_PREUVES", "~/Documents/Technocore-preuves"))
CALENDARS = (
    "https://a.pool.opentimestamps.org",
    "https://b.pool.opentimestamps.org",
    "https://alice.btc.calendar.opentimestamps.org",
)
USER_AGENT = "technocore-preuves/1.0"


class PreuveError(Exception):
    pass


# --------------------------------------------------------------------------- signatures

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(value: str) -> bytes:
    number = 0
    for char in value:
        number = number * 58 + B58.index(char)
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return b"\x00" * (len(value) - len(value.lstrip("1"))) + raw


def public_key_from_did(did: str) -> Ed25519PublicKey:
    if not isinstance(did, str) or not did.startswith("did:key:z"):
        raise PreuveError(f"DID invalide : {did!r}")
    decoded = b58decode(did[len("did:key:z"):])
    if decoded[:2] != b"\xed\x01" or len(decoded) != 34:
        raise PreuveError("ce did:key n'est pas une clé Ed25519")
    return Ed25519PublicKey.from_public_bytes(decoded[2:])


def signature_ok(did: str, sig: str, payload: bytes) -> bool:
    try:
        public_key_from_did(did).verify(base64.urlsafe_b64decode(sig + "=="), payload)
        return True
    except (InvalidSignature, ValueError, PreuveError):
        return False


def message_payload(record: dict) -> bytes:
    return f"{record['room']}|{record['nonce']}|{record['text']}".encode("utf-8")


def contribution_payload(proof: dict) -> bytes:
    # Même forme canonique que technocore_agent.contribution_payload.
    canonical = {
        "artifact_url": proof["artifact_url"],
        "commit": proof["commit"].lower(),
        "schema": "technocore-contribution-v1",
    }
    return json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def verify_record(record: dict) -> bool:
    if record["type"] == "message":
        return signature_ok(record["from"], record["sig"], message_payload(record))
    proof = record["proof"]
    return signature_ok(proof["did"], proof["signature"], contribution_payload(proof))


# --------------------------------------------------------------------------- réseau


def http(url: str, data: bytes | None = None, timeout: float = 30) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if data is not None:
        headers["Content-Type"] = "application/octet-stream"
    with urlopen(Request(url, data=data, headers=headers), timeout=timeout) as response:
        return response.read()


def read_room(room: str, since: int | None = None, limit: int = 200) -> dict:
    query = f"limit={limit}&format=json" + (f"&since={since}" if since is not None else "")
    return json.loads(http(f"{BASE_URL}/r/{room}?{query}"))


def server_confirmation(record: dict) -> dict:
    """Relit le message sur le serveur juste après publication (le lobby l'efface vite).

    Avec since=, le serveur renvoie les 200 messages les PLUS RÉCENTS postérieurs à since :
    la vérification n'est fiable que si elle a lieu peu après la publication.
    """
    checked_at = now_iso()
    try:
        page = read_room(record["room"], since=int(record["seq"]) - 1)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
        return {"checked_at": checked_at, "status": "erreur", "detail": str(error)}
    for message in page.get("messages", []):
        if message.get("seq") == record["seq"]:
            same = message.get("sig") == record["sig"] and message.get("from") == record["from"]
            return {"checked_at": checked_at, "status": "confirme" if same else "different"}
    return {"checked_at": checked_at, "status": "introuvable"}


def find_recent(room: str, did: str, text: str) -> dict | None:
    """Après un délai dépassé, cherche si le message est quand même arrivé."""
    try:
        page = read_room(room)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return None
    for message in reversed(page.get("messages", [])):
        if message.get("from") == did and message.get("text") == text.strip():
            return message
    return None


# --------------------------------------------------------------------------- OpenTimestamps

OTS_MAGIC = b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94"
OTS_UNARY = {0x02: "sha1", 0x03: "ripemd160", 0x08: "sha256", 0x67: "keccak256", 0xF2: "reverse", 0xF3: "hexlify"}
OTS_BINARY = {0xF0: "append", 0xF1: "prepend"}
PENDING_TAG = bytes.fromhex("83dfe30d2ef90c8e")
BITCOIN_TAG = bytes.fromhex("0588960d73d71901")


class Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise PreuveError("fichier .ots tronqué")
        self.pos += 1
        return self.data[self.pos - 1]

    def bytes(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise PreuveError("fichier .ots tronqué")
        self.pos += n
        return self.data[self.pos - n:self.pos]

    def varuint(self) -> int:
        value, shift = 0, 0
        while True:
            b = self.byte()
            value |= (b & 0x7F) << shift
            if not b & 0x80:
                return value
            shift += 7

    def varbytes(self) -> bytes:
        return self.bytes(self.varuint())


def varuint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def apply_op(op: tuple, msg: bytes) -> bytes:
    name = op[0]
    if name == "append":
        return msg + op[1]
    if name == "prepend":
        return op[1] + msg
    if name == "sha256":
        return hashlib.sha256(msg).digest()
    if name == "sha1":
        return hashlib.sha1(msg).digest()
    if name == "ripemd160":
        return hashlib.new("ripemd160", msg).digest()
    if name == "reverse":
        return msg[::-1]
    if name == "hexlify":
        return msg.hex().encode()
    raise PreuveError(f"opération OpenTimestamps non gérée : {name}")


def parse_timestamp(reader: Reader, msg: bytes) -> dict:
    """Arbre {msg, attestations: [(tag, payload)], ops: [(op, sous-arbre)]}."""
    node = {"msg": msg, "attestations": [], "ops": []}

    def item(tag: int) -> None:
        if tag == 0x00:
            att_tag = reader.bytes(8)
            node["attestations"].append((att_tag, reader.varbytes()))
        elif tag in OTS_UNARY:
            op = (OTS_UNARY[tag],)
            node["ops"].append((op, parse_timestamp(reader, apply_op(op, msg))))
        elif tag in OTS_BINARY:
            op = (OTS_BINARY[tag], reader.varbytes())
            node["ops"].append((op, parse_timestamp(reader, apply_op(op, msg))))
        else:
            raise PreuveError(f"étiquette OpenTimestamps inconnue : {tag:#x}")

    tag = reader.byte()
    while tag == 0xFF:
        item(reader.byte())
        tag = reader.byte()
    item(tag)
    return node


def serialize_timestamp(node: dict) -> bytes:
    items = [b"\x00" + tag + varuint(len(payload)) + payload for tag, payload in node["attestations"]]
    tags = {v: k for k, v in {**OTS_UNARY, **OTS_BINARY}.items()}
    for op, child in node["ops"]:
        head = bytes([tags[op[0]]]) + (varuint(len(op[1])) + op[1] if len(op) > 1 else b"")
        items.append(head + serialize_timestamp(child))
    return b"".join(b"\xff" + part for part in items[:-1]) + items[-1]


def walk(node: dict):
    yield node
    for _, child in node["ops"]:
        yield from walk(child)


def ots_status(ots_path: Path) -> str:
    if not ots_path.exists():
        return "absent"
    try:
        _, root = read_ots(ots_path)
    except PreuveError:
        return "illisible"
    tags = {tag for node in walk(root) for tag, _ in node["attestations"]}
    if BITCOIN_TAG in tags:
        return "bitcoin"
    return "en_attente" if PENDING_TAG in tags else "inconnu"


def read_ots(ots_path: Path) -> tuple[bytes, dict]:
    reader = Reader(ots_path.read_bytes())
    if reader.bytes(len(OTS_MAGIC)) != OTS_MAGIC or reader.varuint() != 1 or reader.byte() != 0x08:
        raise PreuveError(f"{ots_path.name} n'est pas un fichier .ots sha256 v1")
    digest = reader.bytes(32)
    return digest, parse_timestamp(reader, digest)


def stamp(path: Path) -> Path:
    digest = hashlib.sha256(path.read_bytes()).digest()
    last_error = None
    for calendar in CALENDARS:
        try:
            response = http(f"{calendar}/digest", data=digest, timeout=20)
            parse_timestamp(Reader(response), digest)  # refuse une réponse invalide
            ots = path.with_name(path.name + ".ots")
            ots.write_bytes(OTS_MAGIC + b"\x01\x08" + digest + response)
            return ots
        except (HTTPError, URLError, TimeoutError, OSError, PreuveError) as error:
            last_error = error
    raise PreuveError(f"aucun calendrier OpenTimestamps n'a répondu ({last_error})")


def upgrade(ots_path: Path) -> bool:
    """Remplace les attestations en attente par la réponse complète du calendrier."""
    digest, root = read_ots(ots_path)
    changed = False
    for node in list(walk(root)):
        pending = [(t, p) for t, p in node["attestations"] if t == PENDING_TAG]
        for tag, payload in pending:
            calendar = Reader(payload).varbytes().decode("utf-8")
            if urlsplit(calendar).scheme != "https":
                continue
            try:
                response = http(f"{calendar.rstrip('/')}/timestamp/{node['msg'].hex()}", timeout=20)
            except HTTPError as error:
                if error.code == 404:  # pas encore ancré dans un bloc Bitcoin
                    continue
                raise
            except (URLError, TimeoutError, OSError):
                continue
            upgraded = parse_timestamp(Reader(response), node["msg"])
            if not any(t == BITCOIN_TAG for n in walk(upgraded) for t, _ in n["attestations"]):
                continue
            node["attestations"].remove((tag, payload))
            node["attestations"].extend(upgraded["attestations"])
            node["ops"].extend(upgraded["ops"])
            changed = True
    if changed:
        ots_path.write_bytes(OTS_MAGIC + b"\x01\x08" + digest + serialize_timestamp(root))
    return changed


# --------------------------------------------------------------------------- archive


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:40] or "x"


class Archive:
    """Dossier de preuves, indexé en mémoire et partageable entre threads.

    Les fichiers restent la source de vérité : `reload()` récupère ceux écrits par un autre
    processus (par exemple `preuves say` lancé pendant que le tableau de bord tourne).
    """

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        (self.root / "messages").mkdir(parents=True, exist_ok=True)
        (self.root / "contributions").mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._items: dict[Path, dict] = {}
        self._keys: set[str] = set()
        self.reload()

    @staticmethod
    def key(record: dict) -> str:
        return record.get("sig") or record.get("proof", {}).get("signature", "")

    def reload(self) -> int:
        """Relit le dossier ; renvoie le nombre de preuves apparues depuis la dernière lecture."""
        with self.lock:
            before = len(self._items)
            for path in sorted(self.root.glob("*/*.json")):
                if path in self._items:
                    continue
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    print(f"  ignoré (illisible) : {path}", file=sys.stderr)
                    continue
                self._items[path] = record
                self._keys.add(self.key(record))
            return len(self._items) - before

    def records(self) -> list[tuple[Path, dict]]:
        with self.lock:
            return sorted(self._items.items())

    def known_dids(self) -> list[str]:
        dids = []
        for _, record in self.records():
            did = record.get("from") or record.get("proof", {}).get("did")
            if did and did not in dids:
                dids.append(did)
        return dids

    def save(self, record: dict) -> Path | None:
        """Écrit une preuve immuable ; renvoie None si elle est déjà archivée."""
        if record["type"] == "message":
            date = record["ts"][:10]
            path = self.root / "messages" / f"{date}_{record['room']}_{record['seq']}.json"
        else:
            proof = record["proof"]
            repo = slug(urlsplit(proof["artifact_url"]).path.rsplit("/", 1)[-1])
            path = self.root / "contributions" / f"{record['captured_at'][:10]}_{repo}_{proof['commit'][:7]}.json"
        key = self.key(record)
        with self.lock:
            if key in self._keys:
                return None
            n = 2
            while path.exists():
                try:  # écrit entre-temps par un autre processus ?
                    other = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    other = {}
                if self.key(other) == key:
                    self._items[path] = other
                    self._keys.add(key)
                    return None
                path = path.with_name(f"{path.stem.rsplit('~', 1)[0]}~{n}.json")
                n += 1
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.chmod(path, 0o444)  # l'horodatage porte sur ces octets : le fichier ne doit plus bouger
            self._items[path] = record
            self._keys.add(key)
            return path


def message_record(posted: dict, room: str, source: str) -> dict:
    record = {
        "type": "message",
        "room": posted.get("room", room),
        **{k: posted[k] for k in ("seq", "ts", "from", "nonce", "text", "sig")},
        "captured_at": now_iso(),
        "source": source,
    }
    record["signature_valide"] = verify_record(record)
    return record


def contribution_record(proof: dict, source: str) -> dict:
    record = {"type": "contribution", "proof": proof, "captured_at": now_iso(), "source": source}
    record["signature_valide"] = verify_record(record)
    return record


def extract_json_objects(text: str) -> list[dict]:
    decoder, objects, i = json.JSONDecoder(), [], 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict):
            objects.append(obj)
        i = end
    return objects


def records_from_object(obj: dict, source: str) -> list[dict]:
    if obj.get("schema") == "technocore-contribution-proof-v1":
        return [contribution_record(obj, source)]
    if obj.get("type") in ("message", "contribution"):  # fichier déjà archivé
        return [obj]
    if isinstance(obj.get("posted"), dict):
        return [message_record(obj["posted"], obj.get("room", "lobby"), source)]
    if {"from", "sig", "nonce", "text", "seq"} <= obj.keys() and obj.get("room"):
        return [message_record(obj, obj["room"], source)]
    return []


# --------------------------------------------------------------------------- rapports


def build_reports(archive: Archive) -> None:
    rows = []
    for path, record in archive.records():
        ots = ots_status(path.with_name(path.name + ".ots"))
        if record["type"] == "message":
            rows.append({
                "type": "message", "date": record["ts"], "did": record["from"],
                "lieu": f"technocore.chat/{record['room']} #{record['seq']}",
                "contenu": record["text"],
                "signature": "valide" if verify_record(record) else "INVALIDE",
                "serveur": record.get("server_confirmation", {}).get("status", "non_verifie"),
                "horodatage": ots, "fichier": str(path.relative_to(archive.root)),
            })
        else:
            proof = record["proof"]
            rows.append({
                "type": "contribution", "date": record["captured_at"], "did": proof["did"],
                "lieu": proof["artifact_url"], "contenu": f"commit {proof['commit']}",
                "signature": "valide" if verify_record(record) else "INVALIDE",
                "serveur": "-", "horodatage": ots, "fichier": str(path.relative_to(archive.root)),
            })
    rows.sort(key=lambda r: r["date"])

    with open(archive.root / "index.jsonl", "w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")

    with open(archive.root / "preuves.csv", "w", encoding="utf-8-sig", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=list(rows[0]) if rows else ["type"], delimiter=";")
        writer.writeheader()
        writer.writerows(rows)

    labels = {"bitcoin": "✅ ancré Bitcoin", "en_attente": "⏳ en attente", "absent": "—"}
    md = ["# Preuves Technocore", "", f"Généré le {now_iso()} · {len(rows)} preuve(s)", "",
          "| Date (UTC) | Type | Où | Contenu | Signature | Serveur | Horodatage |",
          "|---|---|---|---|---|---|---|"]
    for r in rows:
        contenu = r["contenu"].replace("|", "\\|")
        contenu = contenu if len(contenu) <= 90 else contenu[:87] + "…"
        md.append(f"| {r['date'][:19].replace('T', ' ')} | {r['type']} | {r['lieu']} | {contenu} | "
                  f"{r['signature']} | {r['serveur']} | {labels.get(r['horodatage'], r['horodatage'])} |")
    dids = sorted({r["did"] for r in rows})
    md += ["", "## DID", ""] + [f"- `{d}`" for d in dids]
    md += ["", "Chaque fichier `.json` est vérifiable hors ligne (`preuves.py verifier`) ; chaque `.json.ots` "
           "se vérifie aussi sur https://opentimestamps.org en y déposant les deux fichiers.", ""]
    (archive.root / "PREUVES.md").write_text("\n".join(md), encoding="utf-8")

    (archive.root / "preuves.html").write_text(render_html(rows, dids, labels), encoding="utf-8")


def render_html(rows: list[dict], dids: list[str], labels: dict) -> str:
    e = html.escape
    body = "".join(
        f"<tr><td>{e(r['date'][:19].replace('T', ' '))}</td><td>{e(r['type'])}</td><td>{e(r['lieu'])}</td>"
        f"<td>{e(r['contenu'])}</td><td class='{'ok' if r['signature'] == 'valide' else 'ko'}'>{e(r['signature'])}</td>"
        f"<td>{e(r['serveur'])}</td><td>{e(labels.get(r['horodatage'], r['horodatage']))}</td>"
        f"<td><code>{e(r['fichier'])}</code></td></tr>"
        for r in rows
    )
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Preuves Technocore</title>
<style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--line:#e4e1d9;--ok:#1f7a4d;--ko:#b3261e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161614;--fg:#ecebe6;--muted:#9c9a92;--line:#34332f;--ok:#5cc08f;--ko:#ff8a80}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px;max-width:1200px;margin:auto}}
h1{{font-size:22px;margin:0 0 4px}}p{{color:var(--muted);margin:0 0 16px}}
.wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{color:var(--muted);font-weight:600}}code{{font-size:12px;word-break:break-all}}
.ok{{color:var(--ok)}}.ko{{color:var(--ko);font-weight:700}}
</style></head><body>
<h1>Preuves Technocore</h1>
<p>Généré le {e(now_iso())} · {len(rows)} preuve(s)</p>
<p>DID : {"<br>".join(f"<code>{e(d)}</code>" for d in dids) or "—"}</p>
<div class="wrap"><table><thead><tr><th>Date (UTC)</th><th>Type</th><th>Où</th><th>Contenu</th>
<th>Signature</th><th>Serveur</th><th>Horodatage</th><th>Fichier</th></tr></thead><tbody>{body}</tbody></table></div>
</body></html>
"""


# --------------------------------------------------------------------------- commandes


def run_agent(agent_dir: Path, *args: str) -> str:
    agent_dir = agent_dir.expanduser().resolve()
    script = agent_dir / "technocore_agent.py"
    if not script.exists():
        raise PreuveError(f"technocore_agent.py introuvable dans {agent_dir} (option --agent)")
    python = agent_dir / ".venv" / "bin" / "python"
    # stdout est capturé ; la passphrase est lue sur le terminal (/dev/tty) par l'agent lui-même.
    result = subprocess.run(
        [str(python if python.exists() else sys.executable), str(script), *args],
        cwd=agent_dir, stdout=subprocess.PIPE, text=True,
    )
    if result.returncode != 0:
        raise PreuveError(f"technocore_agent.py a échoué (code {result.returncode})")
    return result.stdout


def archive_new(archive: Archive, records: list[dict], ots: bool) -> int:
    added = 0
    for record in records:
        path = archive.save(record)
        if path is None:
            print(f"  déjà archivé : {describe(record)}")
            continue
        added += 1
        status = "signature valide" if record["signature_valide"] else "⚠️ SIGNATURE INVALIDE"
        print(f"  ✅ archivé : {describe(record)} → {path.relative_to(archive.root)} ({status})")
        if ots:
            try:
                stamp(path)
                print("     horodatage OpenTimestamps demandé (ancrage Bitcoin sous ~quelques heures)")
            except PreuveError as error:
                print(f"     horodatage impossible pour l'instant : {error} → relance `horodater` plus tard")
    return added


def describe(record: dict) -> str:
    if record["type"] == "message":
        return f"message {record['room']} #{record['seq']}"
    return f"contribution {record['proof']['artifact_url']} @ {record['proof']['commit'][:7]}"


def cmd_say(args, archive: Archive) -> None:
    started = time.time()
    try:
        output = run_agent(args.agent, "say", args.room, args.text, "--timeout", str(args.timeout))
    except PreuveError:
        dids = archive.known_dids()
        print("\nL'envoi a échoué ou a expiré. Je vérifie si le message est quand même arrivé…", flush=True)
        for did in dids:
            found = find_recent(args.room, did, args.text)
            if found:
                print("Il est bien arrivé sur le serveur.")
                record = message_record({**found, "room": args.room}, args.room, "say-recupere")
                record["server_confirmation"] = {"checked_at": now_iso(), "status": "confirme"}
                archive_new(archive, [record], not args.sans_horodatage)
                build_reports(archive)
                return
        hint = "" if dids else " (aucun DID connu dans l'archive : importe d'abord une preuve)"
        raise PreuveError(f"message introuvable sur le serveur{hint} ; tu peux relancer la même commande")
    objs = [o for o in extract_json_objects(output) if isinstance(o.get("posted"), dict)]
    if not objs:
        raise PreuveError("réponse du serveur sans bloc « posted » : rien à archiver")
    record = message_record(objs[-1]["posted"], objs[-1].get("room", args.room), "say")
    record["server_confirmation"] = server_confirmation(record)
    print(f"Publié en {time.time() - started:.0f} s. Vérification serveur : {record['server_confirmation']['status']}")
    archive_new(archive, [record], not args.sans_horodatage)
    build_reports(archive)


def cmd_proof(args, archive: Archive) -> None:
    output = run_agent(args.agent, "proof", args.url, args.commit)
    objs = [o for o in extract_json_objects(output) if o.get("schema") == "technocore-contribution-proof-v1"]
    if not objs:
        raise PreuveError("technocore_agent.py n'a pas renvoyé de preuve")
    archive_new(archive, [contribution_record(objs[-1], "proof")], not args.sans_horodatage)
    build_reports(archive)


def cmd_importer(args, archive: Archive) -> None:
    sources: list[tuple[str, str]] = []
    if args.fichiers:
        for name in args.fichiers:
            path = Path(name).expanduser()
            files = sorted(path.rglob("*.json")) if path.is_dir() else [path]
            sources += [(str(f), f.read_text(encoding="utf-8", errors="replace")) for f in files]
    else:
        try:
            clip = subprocess.run(["pbpaste"], stdout=subprocess.PIPE, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError) as error:
            raise PreuveError("presse-papiers inaccessible : passe un fichier en argument") from error
        sources.append(("presse-papiers", clip))
    records = []
    for name, text in sources:
        found = [r for obj in extract_json_objects(text) for r in records_from_object(obj, "import")]
        print(f"{name} : {len(found)} preuve(s) trouvée(s)")
        records += found
    if not records:
        raise PreuveError("aucune preuve reconnue (sortie de `say`, bloc « posted » ou contribution-proof.json)")
    archive_new(archive, records, not args.sans_horodatage)
    build_reports(archive)


def cmd_horodater(args, archive: Archive) -> None:
    todo = [p for p, _ in archive.records() if not p.with_name(p.name + ".ots").exists()]
    for path in todo:
        try:
            stamp(path)
            print(f"  ⏳ horodatage demandé : {path.name}")
        except PreuveError as error:
            print(f"  ✗ {path.name} : {error}")
    if not todo:
        print("Tout est déjà horodaté.")
    build_reports(archive)


def cmd_completer(args, archive: Archive) -> None:
    for path, _ in archive.records():
        ots = path.with_name(path.name + ".ots")
        if ots_status(ots) != "en_attente":
            continue
        try:
            done = upgrade(ots)
        except (PreuveError, HTTPError) as error:
            print(f"  ✗ {path.name} : {error}")
            continue
        print(f"  {'✅ ancré dans Bitcoin' if done else '⏳ pas encore ancré'} : {path.name}")
    build_reports(archive)


def cmd_verifier(args, archive: Archive) -> None:
    bad = 0
    for path, record in archive.records():
        ok = verify_record(record)
        bad += not ok
        print(f"  {'✅' if ok else '❌'} {describe(record)} ({path.name}) · horodatage : "
              f"{ots_status(path.with_name(path.name + '.ots'))}")
    build_reports(archive)
    if bad:
        raise PreuveError(f"{bad} signature(s) invalide(s)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="preuves.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE, help=f"dossier d'archive ({DEFAULT_ARCHIVE})")
    parser.add_argument("--agent", type=Path, default=DEFAULT_AGENT_DIR, help=f"dossier technocore-did-starter ({DEFAULT_AGENT_DIR})")
    parser.add_argument("--sans-horodatage", action="store_true", help="ne pas contacter OpenTimestamps")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("say", help="publie un message signé puis l'archive")
    p.add_argument("room")
    p.add_argument("text")
    p.add_argument("--timeout", type=float, default=60)
    p = sub.add_parser("proof", help="signe une contribution Git puis l'archive")
    p.add_argument("url")
    p.add_argument("commit")
    p = sub.add_parser("importer", help="archive des sorties existantes (presse-papiers par défaut)")
    p.add_argument("fichiers", nargs="*")
    sub.add_parser("horodater", help="horodate les preuves qui ne le sont pas encore")
    sub.add_parser("completer", help="récupère les ancrages Bitcoin des horodatages en attente")
    sub.add_parser("verifier", help="revérifie toutes les signatures")
    sub.add_parser("rapport", help="régénère PREUVES.md, preuves.csv, preuves.html, index.jsonl")
    args = parser.parse_args(argv)

    archive = Archive(args.archive)
    commands = {
        "say": cmd_say, "proof": cmd_proof, "importer": cmd_importer, "horodater": cmd_horodater,
        "completer": cmd_completer, "verifier": cmd_verifier, "rapport": lambda a, ar: build_reports(ar),
    }
    try:
        commands[args.cmd](args, archive)
    except PreuveError as error:
        print(f"erreur : {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print(f"\nArchive : {archive.root}  (rapport : {archive.root / 'preuves.html'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
