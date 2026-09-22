# Tuto : prendre en main Technocore Preuves (version ELI5 🧒)

## L'idée en 30 secondes

Imagine un **grand tableau noir** dans une salle où tout le monde écrit en même temps.
Toutes les heures, quelqu'un **l'efface**.

- Le tableau noir, c'est **Technocore**.
- Ta **signature**, c'est ton **DID** (`did:key:z6Mk…`) : elle prouve que c'est bien toi qui as
  écrit la phrase.
- Le problème : si personne n'a pris de photo avant que le tableau soit effacé, **impossible de
  prouver** que tu as écrit quelque chose.

**Technocore Preuves**, c'est un ami assis au fond de la salle qui :

1. 📸 **photographie** chaque phrase signée par toi, dès qu'elle apparaît ;
2. 🔍 **vérifie** que la signature est bien la tienne et pas une imitation ;
3. 🏛️ fait **dater la photo par un notaire**, ici Bitcoin, via OpenTimestamps. Ni toi ni le serveur
   ne pourrez changer cette date ;
4. 🗂️ **range** tout dans un classeur où tu retrouves n'importe quelle phrase en tapant un mot.

Et cet ami ne touche **jamais** à ton stylo : ta clé privée et ta passphrase ne passent pas par
l'outil.

---

## Ce qu'il te faut

- Un Mac (Linux marche aussi ; Windows, voir la fin)
- Ton **DID** : la ligne `did:key:z6Mk…`. Pas encore de DID ? Suis d'abord le
  [guide FR](https://github.com/BgeorgesEth/guide-technocore-fr).
- 5 minutes

---

## Étape 1 : télécharger

Ouvre le **Terminal** (Cmd + Espace, puis tape « Terminal ») et colle :

```bash
cd ~/Desktop && git clone https://github.com/BgeorgesEth/technocore-preuves
```

> 💡 Si le Mac propose d'installer les « outils de développement », accepte, puis relance la
> commande.

## Étape 2 : installer (une seule fois)

```bash
cd ~/Desktop/technocore-preuves && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
chmod +x tableau preuves "Tableau Technocore.command"
```

Traduction : on crée une petite boîte isolée pour l'outil, on y installe la seule bibliothèque
dont il a besoin (`cryptography`, pour vérifier les signatures), puis on autorise le lancement
des raccourcis.

## Étape 3 : lancer

Dans le Finder, ouvre le dossier **Bureau**, puis **technocore-preuves**, et **double-clique sur
`Tableau Technocore.command`**.

> 💡 Si macOS dit « développeur non identifié » : fais un **clic droit** sur le fichier, choisis
> **Ouvrir**, puis **Ouvrir** encore. Ce n'est demandé qu'une fois.

Une fenêtre Terminal s'ouvre, puis ton navigateur affiche le tableau de bord.
**Ne ferme pas la fenêtre Terminal** : c'est elle qui fait travailler l'ami au fond de la salle.

## Étape 4 : lui dire qui tu es

1. Clique sur l'onglet **Surveillance**.
2. Dans **Mes DID**, colle ton `did:key:z6Mk…`, puis clique sur **Ajouter**. Ton DID est public,
   tu ne dévoiles aucun secret.
3. Dans **Salons surveillés**, `technocore` et `lobby` sont déjà là. Ajoute les autres salons où
   tu écris.

C'est fini ! 🎉 L'outil relit tout ce que le serveur garde encore et y retrouve tes messages, puis
il capture les nouveaux au fil de l'eau.

---

## Au quotidien

| Je veux… | Je fais… |
|---|---|
| Retrouver un message | Onglet **Preuves**, puis je tape un mot, un salon ou un DID dans la barre de recherche |
| Savoir si c'est prouvé | Je regarde les badges : ✓ signature · ⏳ horodatage en attente · ⛓️ ancré dans Bitcoin (après quelques heures) |
| Publier un message | Onglet **Ajouter**, **Publier**. Je copie la commande, je la lance dans le Terminal et je tape ma passphrase. Le message apparaît tout seul |
| Ajouter une vieille preuve | Onglet **Ajouter**, **Importer**. Je colle la sortie d'une ancienne commande, ou je dépose un fichier `.json` |
| Tout sauvegarder | Onglet **Exporter**, **Tout (.zip)** |

Tes preuves sont rangées dans **Documents**, puis **Technocore-preuves** : un fichier par message,
plus son horodatage `.ots`. Tout y est public, tu peux le mettre sur iCloud ou Drive sans risque.

---

## Les 3 règles d'or

1. **L'ami ne voit que ce qui se passe quand il est là.** Tableau de bord fermé ou Mac en veille :
   il ne capture rien. Relance-le au moins toutes les 40 minutes si tu écris dans le `lobby`, et
   toutes les 2 heures pour `technocore`, pour qu'il rattrape ce qu'il a manqué.
2. **Ce qui est effacé avant la photo est perdu pour tout le monde.** Personne ne peut le
   récupérer, pas même Flop Labs.
3. **Ne donne jamais** ton fichier `identity.pem` ni ta passphrase, à personne, même à une IA.

---

## Questions fréquentes

**Est-ce que ça garantit un airdrop ?**
Non. L'outil garde des **preuves** de ce que tu as publié. Flop Labs n'a publié aucun critère
officiel pour l'instant.

**Est-ce que l'outil peut publier à ma place ?**
Non, et c'est voulu. Signer demande ta passphrase, et elle reste dans le Terminal.

**Il affiche « non lus » à côté d'un salon, c'est grave ?**
Le salon était trop rapide et quelques messages n'ont pas été lus. Relance le tableau de bord :
il relira l'historique encore disponible.

**Et sous Windows ?**
Les raccourcis ne marchent pas. Après l'installation, lance `python tableau.py` dans le dossier.

**Comment vérifier une preuve sans l'outil ?**
Pour la date : dépose le `.json` et son `.ots` sur https://opentimestamps.org. Pour la signature :
le [guide FR](https://github.com/BgeorgesEth/guide-technocore-fr) fournit un petit script.
