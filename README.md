# Scan Book

Numériser un livre page par page sous Windows (scanner WIA), avec redressement et recadrage automatiques,
aperçu de correction, sessions reprenables et export PDF.

## Prérequis

- Windows et le pilote WIA du scanner (testé : Epson Perfection V330)
- [uv](https://docs.astral.sh/uv/getting-started/installation/) et un Python 3.12 avec Tcl/Tk

Les dépendances s'installent toutes seules au premier `uv run`.

## Utilisation

```powershell
uv run tools/scan_book.py devices                # lister les scanners
uv run tools/scan_book.py scan les-veilleurs     # scanner (ou reprendre) un livre
uv run tools/scan_book.py correct les-veilleurs --page 12
uv run tools/scan_book.py export les-veilleurs   # (re)générer le PDF
```

Poser une page sur la vitre, puis :

| Touche | Action |
|---|---|
| Entrée | Scanner la page suivante |
| C | Corriger la dernière page (angle, rotation, recadrage) |
| R | Rescanner la dernière page |
| M | Basculer couleur / niveaux de gris pour les prochains scans |
| F | Créer le PDF et quitter |
| Q | Quitter sans PDF (la session est conservée) |

Relancer la même commande reprend le livre. Options utiles :

| Option | Effet |
|---|---|
| `--dpi 600` | Résolution (300 par défaut) |
| `--color gray` | Couleur de départ |
| `--area x,y,l,h` | Zone en mm depuis le coin de la vitre (`full` pour toute la vitre) |
| `--scanner "EPSON"` | Choix du scanner |
| `--root "E:\Scans"` | Dossier des livres (`scans/` par défaut) |

## Traitement automatique

Chaque page est traitée dans cet ordre :
1. remise à l'endroit si elle est à l'envers (détection locale du texte avec RapidOCR) ;
2. redressement ;
3. recadrage sur les bords du papier.

Les cas douteux sont signalés **À VÉRIFIER**, et l'original reste dans `raw/`.

Le script ne fait pas d'OCR et ne sépare pas les doubles pages. Il ne corrige pas non plus la courbure de reliure.

## Variantes de recadrage (expérimentales)

Les trois scripts partagent les mêmes sessions et les mêmes dossiers. On peut passer de l'un à l'autre en cours
de livre.

- **`tools/scan_book_deeplab.py`** : segmentation DeepLabV3-ResNet50 (PyTorch, GPU si disponible).
  - Les poids sont téléchargés une fois dans `.cache/`.
  - Les découpes qui coupent du texte sont rejetées.
- **`tools/scan_book_qwen.py`** : recadrage par Qwen3.5 9B via [Ollama](https://ollama.com) en local.
  - Le parcours se fait en trois temps : scan en masse, vérification page par page, puis export.
  - Une session Qwen en cours doit être reprise avec ce même script.

## Fichiers

```text
scans/<livre>/
    session.json      ordre des pages, réglages, transformations
    raw/0001.png      acquisitions brutes
    pages/0001.png    pages corrigées
    <livre>.pdf
```

Un verrou empêche deux commandes de modifier le même livre en même temps. En cas d'interruption, `.pending` est
récupéré au lancement suivant : ne pas le supprimer.

## Banc OCR

`ocr-bench/` compare Marker 2, Claude Haiku 5.5 et GPT-6 Luna sur des pages scannées. Voir
[ocr-bench/SYNTHESE.md](ocr-bench/SYNTHESE.md).

## Tests

```powershell
uv run tests/test_scan_book.py          # ni scanner ni GPU requis
uv run tests/test_scan_book_deeplab.py
uv run tests/test_scan_book_qwen.py
uv run tests/test_ocr_bench.py
```

Définir `SCAN_BOOK_SKIP_GUI=1` pour ignorer les tests Tkinter sur une machine sans bureau.

## Dépannage

- **Aucun scanner trouvé** :
  - allumer le scanner et vérifier l'USB ;
  - fermer les autres logiciels de numérisation ;
  - vérifier le service avec `Get-Service stisvc`.
- **`Can't find a usable init.tcl`** : lancer depuis un terminal Windows normal, ou ajouter `--python 3.12` à `uv run`.
- **Accents illisibles dans PowerShell 5** : lire les fichiers avec `Get-Content -Encoding UTF8`.
