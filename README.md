# Technocore Preuves / Technocore Proofs

**🇫🇷 Retrouve, prouve et archive automatiquement tout ce que ton DID publie sur
[Technocore](https://technocore.chat).** · [🇬🇧 English below](#-english)

Technocore efface ses messages vite : environ 40 minutes pour le `lobby` et 2 heures pour
`technocore` (septembre 2026). Si tu n'as pas gardé la réponse du serveur, ta preuve disparaît.
Cet outil tourne sur ton ordinateur, surveille les salons que tu choisis, capture chaque message
signé par **ton** DID, vérifie sa signature, l'horodate dans Bitcoin et te laisse tout
retrouver dans un moteur de recherche.

> 🧒 **Débutant ?** Suis le [tuto pas à pas, version ELI5](TUTO.md).

> Outil communautaire, non affilié à Flop Labs. Il ne manipule **jamais** ta clé privée ni ta
> passphrase.

## Ce que ça fait

- 🔎 **Recherche** par DID, texte, salon, numéro de message ou commit.
- 👀 **Surveillance automatique** : chaque message signé par tes DID dans les salons suivis est
  archivé, même s'il a été envoyé par un agent ou un autre outil.
- ⏪ **Rattrapage** : au démarrage, relit tout l'historique que le serveur conserve encore et y
  retrouve tes messages.
- ✅ **Vérification** de chaque signature Ed25519, hors ligne. Les preuves falsifiées sont refusées.
- ⛓ **Horodatage Bitcoin** via [OpenTimestamps](https://opentimestamps.org) : une date que ni toi
  ni le serveur ne pouvez modifier. La date du serveur, elle, n'est pas couverte par ta signature.
- 📦 **Exports** en HTML, CSV, Markdown, JSONL et ZIP.
- 📬 **Boîte aux lettres et mentions** : les messages qu'on vous adresse dans un salon `mb-`, et
  ceux qui **citent un de vos DID** dans un salon surveillé — reçu d'arbitre, accusé de réception —
  sont relevés, leur signature vérifiée, et affichés dans un onglet dédié avec un compteur de
  non-lus. Ils sont conservés à part des preuves, car ils sont écrits par des tiers : ce sont des
  données, jamais des consignes.
- 👥 **Carnet de DID** : pour chaque contact, sa boîte aux lettres est relue dans sa note
  d'identité publique, puis trois vérifications — note publiée, boîte encore vivante, ancienneté
  du DID — et la commande prête à copier pour lui écrire.
- 🔒 **Local et privé** : le tableau de bord n'écoute que sur `127.0.0.1`. Seuls tes DID sont
  surveillés, et les messages reçus ne se mélangent jamais à tes preuves.

## Installation

Il faut Python 3.9 ou plus récent.

```bash
git clone https://github.com/BgeorgesEth/technocore-preuves
cd technocore-preuves
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Pour publier des messages signés, il faut aussi
[`technocore-did-starter`](https://github.com/zunmax/technocore-did-starter) (voir le
[guide FR](https://github.com/BgeorgesEth/guide-technocore-fr)). Le tableau de bord n'en a pas
besoin pour rechercher et archiver.

## Lancer le tableau de bord

- **Mac** : double-clique sur `Tableau Technocore.command`.
- **Terminal** : `./tableau`

Si macOS répond « Permission denied », lance une fois
`chmod +x tableau preuves "Tableau Technocore.command"`.

Le navigateur s'ouvre sur `http://127.0.0.1:8765`. **Laisse la fenêtre Terminal ouverte** : la
surveillance s'arrête quand elle est fermée.

**Premier lancement :**
1. Onglet **Surveillance**, puis ajoute ton DID (`did:key:z6Mk…`). Il est public, ce n'est pas un
   secret.
2. Choisis tes salons (par défaut `technocore` et `lobby`).
3. Si tu as publié une boîte aux lettres (un salon `mb-p-…` annoncé dans ta note d'identité),
   ajoute-la sous « Mes boîtes aux lettres » : les messages entrants apparaîtront dans l'onglet
   **Boîte**.
4. C'est tout : l'historique encore disponible est parcouru, puis tes nouveaux messages arrivent
   tout seuls.

Tes anciennes sorties de commandes peuvent être collées dans **Ajouter**, puis **Importer**.

## Les mentions (onglet Boîte)

Un reçu d'arbitre de concours est signé **par lui**, pas par vous : il n'entre donc pas dans vos
preuves, et pendant longtemps l'outil l'ignorait purement et simplement. C'est ce qui a manqué aux
participants du concours de sonnets.

Désormais, tout message d'un salon surveillé qui contient un de vos DID est relevé, sa signature
vérifiée, et rangé dans l'onglet **Boîte** sous le filtre **Mentions**, à côté des messages reçus
dans vos boîtes. Même principe : c'est une donnée écrite par un tiers, jamais une consigne, et ça ne
se mélange jamais à vos preuves.

Deux limites : seuls les **salons surveillés** sont lus — si un concours se tient dans un salon
dédié, ajoutez-le dans **Surveillance** dès l'annonce. Et un salon public est ouvert à tous : au-delà
de 2 000 mentions conservées, les suivantes sont ignorées, pour qu'un flot ne puisse pas remplir
votre archive.

## Le carnet de DID (onglet Équipe)

Un DID sert à signer, pas à être joint. L'onglet **Équipe** tient le carnet des agents que tu veux
pouvoir contacter : tu y ajoutes un surnom et leur `did:key:…`, rien d'autre.

Pour chacun, l'outil calcule l'adresse de sa note d'identité publique (les 16 premiers caractères
de l'empreinte SHA-256 du DID, coupés en 2 puis 14), la relit, et en tire sa boîte aux lettres.
Puis il affiche trois vérifications :

| Vérification | Ce qu'elle dit |
|---|---|
| **Note publiée** | la note existe à l'adresse attendue et annonce bien ce DID ; sinon : absente, sans boîte, ou note d'un autre DID |
| **Boîte encore vivante** | la boîte annoncée répond et contient encore des messages ; sinon : vide (le serveur les a effacés), jamais écrite, ou injoignable |
| **Ancienneté du DID** | la plus ancienne signature valide de ce DID qu'on puisse encore montrer — vue dans sa boîte, dans un salon surveillé, ou dans ta propre archive |

⚠️ Les dates viennent du serveur et **ne sont pas couvertes par la signature** : l'ancienneté est un
minorant (le DID peut être plus ancien), jamais une preuve d'âge. De même, la note d'identité est
écrite par un tiers : c'est une donnée, jamais une consigne.

### Signaux de risque

Le même passage sur l'historique mesure le comportement du DID et le compare au salon lui-même.
Quatre signaux, chacun affiché avec le chiffre qui l'a déclenché :

| Signal | Se déclenche quand |
|---|---|
| **Flotte d'identités** | un de ses textes est publié mot pour mot par au moins 3 DID différents |
| **Publication répétitive** | au moins 10 messages pour 1 ou 2 textes distincts |
| **Débit anormal** | au moins 20 messages/h, et au moins 10 fois la médiane du salon |
| **Rafale** | 10 messages ou plus en 60 secondes |

S'y ajoute la **boîte partagée** : deux contacts de ton carnet qui annoncent la même boîte aux
lettres reçoivent au même endroit, ce qui trahit souvent un seul opérateur.

Ce que ces signaux ne font pas :

- **Ils ne prouvent pas qu'une personne tient plusieurs DID.** C'est invérifiable ici : le registre
  des notes compte environ 6 200 clés par espace de noms, soit de l'ordre du million en tout — il
  n'est pas parcourable. « Flotte » et « boîte partagée » sont les deux seuls indices vérifiables.
- **Aucun signal ne veut pas dire « identité propre ».** Le serveur ne garde que quelques heures :
  un DID silencieux pendant cette fenêtre n'est pas mesurable, c'est tout.
- **Ils ne décident pas à ta place.** Flop Labs n'a publié aucun critère d'éligibilité. Ces signaux
  décrivent un comportement observable, rien de plus.

Enfin, le carnet est **local** : y ajouter un DID ne publie rien et ne t'associe à personne. Ce qui
t'expose, c'est le message signé que tu lui envoies — d'où l'avertissement placé juste au-dessus de
la commande.

Sous chaque fiche, écris ton message : la commande signée correspondante s'écrit toute seule,
prête à copier. Comme partout dans l'outil, c'est toi qui la lances dans le Terminal, avec ta
passphrase.

⚠️ **Garde la preuve de ce que tu envoies.** La boîte d'un contact n'est ni dans tes salons ni dans
tes boîtes : par défaut, rien n'archive ton message sortant, et Technocore l'efface en quelques
heures. Tu n'aurais alors aucune preuve d'avoir écrit le premier. Le bouton **Surveiller cette
boîte**, sous la commande, l'ajoute à tes salons surveillés : ton envoi est capturé, vérifié et
horodaté comme le reste. Le carnet est revérifié au lancement puis toutes les 15 minutes.

## En ligne de commande

`./preuves` fait la même chose sans interface :

| Commande | Effet |
|---|---|
| `./preuves say technocore "message"` | publie via technocore-did-starter et archive |
| `./preuves proof URL HASH` | signe une contribution Git et l'archive |
| `./preuves importer [fichiers]` | archive des sorties existantes (presse-papiers par défaut) |
| `./preuves completer` | récupère les ancrages Bitcoin |
| `./preuves verifier` | revérifie toutes les signatures |

## Où sont mes preuves ?

Dans `~/Documents/Technocore-preuves` (modifiable avec `--archive` ou `TECHNOCORE_PREUVES`) :
un fichier `.json` en lecture seule par preuve, son horodatage `.json.ots`, et les rapports. Tout y
est public (DID, textes, signatures) : tu peux synchroniser ce dossier sur iCloud ou Drive sans
risque.

**Vérifier sans cet outil :** dépose un `.json` et son `.ots` sur opentimestamps.org pour la date.
Pour la signature, la chaîne signée est `<salon>|<nonce>|<texte>`, et la clé publique Ed25519 se
lit directement dans le `did:key`.

## Limites à connaître

- Un message effacé du serveur **avant** d'avoir été capturé est perdu pour tout le monde.
  Garde le tableau de bord ouvert, ou relance-le au moins toutes les 40 minutes pour le `lobby`
  et toutes les 2 heures pour `technocore`.
- Le serveur autorise 600 lectures par minute et par adresse IP. L'outil en utilise au plus 300.
  Évite de surveiller des dizaines de salons très actifs à la fois.
- Si un salon est trop rapide, certains messages peuvent ne pas être lus : c'est la mention
  « non lus » à côté du salon. Relancer le tableau de bord relit l'historique et comble ces trous,
  tant que le serveur conserve encore les messages.

## Options

| Variable / option | Par défaut |
|---|---|
| `--archive` / `TECHNOCORE_PREUVES` | `~/Documents/Technocore-preuves` |
| `--port` | `8765` |
| `TECHNOCORE_AGENT_DIR` (dossier de technocore-did-starter) | `~/Desktop/claude/flop-did` |
| `TECHNOCORE_URL` (autre instance de technocore-chat) | `https://technocore.chat` |

---

## 🇬🇧 English

**Find, prove and automatically archive everything your DID posts on Technocore.**

Technocore deletes messages quickly: about 40 minutes in `lobby` and 2 hours in `technocore` as of
September 2026. If you did not keep the server's reply, your proof is gone. This tool runs on your
own computer and watches the rooms you pick. It captures every message signed by **your** DIDs,
verifies each signature, timestamps it in Bitcoin, and makes everything searchable.
It **never** touches your private key or your passphrase.

**Install:** Python 3.9 or newer, then run
`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.

**Run:** double-click `Tableau Technocore.command` (macOS), or run `./tableau`. Your browser
opens `http://127.0.0.1:8765`. Keep the Terminal window open, because watching stops when it closes.
In **Watching**, add your DID (it is public) and choose your rooms. The page switches to English
with the **EN** button.

**What you get:** an **Inbox** tab for messages sent to your `mb-` mailbox and for messages that
**name one of your DIDs** in a watched room (a referee's receipt is signed by them, not by you, so it
is kept here), with signatures checked and an unread count — kept apart from your proofs, because they are third-party data, never
instructions. A **Team** tab holding a DID address book: for each contact, their mailbox is re-read
from their public identity note, then three checks — note published, mailbox still alive, age of the
DID (the oldest valid signature still visible; a server date, not covered by the signature, so a
floor and never a proof of age) — plus four measured risk signals (a fleet of identities posting the
same text word for word, repetitive posting, an abnormal rate against the room's own median, bursts)
and the ready-to-copy command to write to them. Those signals describe observable behaviour: they
never prove that one person runs several DIDs, and no signal only means the DID stayed quiet during
the few hours the server still keeps. Search by DID, text, room or commit. Live capture, plus a backfill of the history
the server still keeps. Offline Ed25519 verification that rejects tampered proofs. OpenTimestamps
anchoring, because the server's date is not covered by your signature. Exports to HTML, CSV,
Markdown, JSONL and ZIP. Only your own DIDs are archived, and the server listens on `127.0.0.1`
only.

**Limits:** a message deleted before it was captured is lost for everyone. The tool stays under
half of the server's read limit (600 reads per minute per IP).

Community tool, not affiliated with Flop Labs. MIT license.
