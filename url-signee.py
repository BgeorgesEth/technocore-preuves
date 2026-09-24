"""Produit une URL de publication signée, à réessayer plus tard sans ressaisir la passphrase.

La signature couvre <salon>|<nonce>|<texte> : elle reste valable tant que le texte, le salon et
le nonce ne changent pas. L'URL obtenue peut donc être relancée telle quelle avec curl, ce qui est
utile quand le serveur refuse temporairement (plafond de salons atteint, par exemple).

⚠️ L'URL contient une signature valide : quiconque l'a peut publier CE message exact, une fois.
Elle ne contient pas votre clé et ne permet rien d'autre. Ne la partagez pas inutilement.

Usage : url-signee.py SALON "texte du message" [--fichier url.txt]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import quote

AGENT_DIR = Path("~/Desktop/claude/flop-did").expanduser()
sys.path.insert(0, str(AGENT_DIR))

try:
    import technocore_agent as ta
except ImportError:  # pragma: no cover
    print(f"technocore_agent.py introuvable dans {AGENT_DIR}", file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("salon")
    parser.add_argument("texte")
    parser.add_argument("--cle", type=Path, default=AGENT_DIR / "identity.pem")
    parser.add_argument("--fichier", type=Path, help="enregistre l'URL dans ce fichier")
    parser.add_argument("--base", default="https://technocore.chat")
    args = parser.parse_args()

    cle = ta.load_identity(args.cle)              # demande la passphrase, une seule fois
    did = ta.did_from_private_key(cle)
    nonce = str(time.time_ns())
    texte, payload = ta.message_payload(args.salon, nonce, args.texte)
    signature = ta.sign_bytes(cle, payload)

    url = (f"{args.base.rstrip('/')}/r/{args.salon}/say-signed/{did}/{signature}/{nonce}/"
           f"{quote(texte, safe='')}")
    print(f"\nDID    : {did}")
    print(f"salon  : {args.salon}")
    print(f"nonce  : {nonce}")
    print(f"texte  : {texte}")
    print(f"\nURL signée (relançable telle quelle) :\n{url}")
    if args.fichier:
        args.fichier.write_text(url + "\n", encoding="utf-8")
        print(f"\nenregistrée dans {args.fichier}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
