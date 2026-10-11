# Scan Book

[English](README.md) | **Français**

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

Les pages douteuses sont signalées « À VÉRIFIER » et le scan d'origine reste dans `raw/`.

Pas d'OCR, pas de séparation des doubles pages, pas de correction de la courbure de reliure.

## Trois variantes de recadrage

Les trois scripts prennent les mêmes commandes et les mêmes touches, et partagent sessions, dossiers, PDF et verrou.
Pour changer de script en cours de livre, attendre la fin de l'acquisition, quitter avec Q et relancer le même livre
avec l'autre script. Seul le recadrage change ; la remise à l'endroit et le redressement sont identiques.

| | `scan_book.py` (original) | `scan_book_deeplab.py` | `scan_book_qwen.py` |
|---|---|---|---|
| Recadrage | Heuristiques d'image (contour du papier, ombre de bord, fond du scanner) | Segmentation DeepLabV3-ResNet50 (poids [LearnOpenCV](https://github.com/spmallick/learnopencv/tree/master/Document-Scanner-Custom-Semantic-Segmentation-using-PyTorch-DeepLabV3)) | Qwen3.5 9B en local via [Ollama](https://ollama.com), qui renvoie un rectangle |
| Marge conservée | 3 mm | 0,5 mm | 0,5 mm |
| Garde-fou | Limite incertaine conservée | Découpe rejetée si elle coupe du texte détecté | Réponse incertaine ou zone < 20 % rejetée |
| Dépendances | Aucune lourde | PyTorch CUDA (~3 Go), poids de 161 Mo téléchargés dans `.cache/` | Ollama démarré, modèle `qwen3.5:9b` (~6,6 Go) |
| Vitesse | Immédiat | Rapide sur GPU | 2,5 à 3 s par page une fois le modèle chargé |
| Déroulé | Chaque page est traitée et affichée avant la suivante | Idem | Scan en masse, traitement en tâche de fond, puis vérification page par page avant l'export |

Tout le traitement reste local : aucune image n'est envoyée à un service distant.

Les deux variantes n'ont été essayées que sur 4 pages. DeepLab confond parfois la page avec ses blocs imprimés :
le garde-fou rejette alors souvent sa découpe et la page reste à recadrer à la main, si bien qu'il ne fait pas mieux
que l'original. Qwen recadre correctement les pages intérieures, mais ses coordonnées sont approximatives et le cadre
peut être serré près des titres : vérifier l'aperçu.

Pendant la vérification Qwen, Entrée valide une page et C la corrige. F n'exporte qu'une fois toutes les pages
validées, et une page dont l'image a été supprimée de `pages/` est laissée hors du PDF. Une session Qwen inachevée se
reprend avec le script Qwen : les deux autres ignorent ses pages en attente.

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

Les PDF produits n'ont pas de couche texte. `ocr-bench/` compare trois moteurs qui pourraient en ajouter une :
Marker 2 (en local, sur GPU), Claude Haiku 5.5 et GPT-6 Luna via AWS Bedrock. Ils ont tourné sur 16 pages de nos
scans, notées contre une transcription de référence, et sur 20 pages
d'[olmOCR-bench](https://huggingface.co/datasets/allenai/olmOCR-bench), notées sur ses extraits vérifiés par des
humains. Les scores sont des taux d'erreur par caractère (CER), plus bas = meilleur :

| | Marker 2 (force_ocr) | Claude Haiku 5.5 | GPT-6 Luna |
|---|---|---|---|
| Nos scans (hors page difficile) | 0,72 % | **0,35 %** | 0,37 % |
| Page en police manuscrite stylisée | 1,1 % (vide en mode auto) | **0,75 %** | 1,6 % |
| olmOCR, extraits | 1,0 % | **0,46 %** | 0,80 % |
| Temps médian par page | 18 s | ≈ 38 s (via agent) | 17 s |

Haiku arrive en tête. Il garde aussi les titres de page et de chapitre, et ne demande aucune infrastructure
locale. En mode auto, Marker 2 a rendu une page vide sans signaler d'erreur ; il supprime les titres même avec
`force_ocr` et a besoin d'un serveur vLLM dans Docker. Luna corrige sans prévenir les coquilles de l'imprimé et
refuse `temperature=0`.

[SYNTHESE.md](ocr-bench/SYNTHESE.md) reprend les paramètres à réutiliser (taille d'image, consigne, contrôles des
sorties), [RESULTATS.md](ocr-bench/RESULTATS.md) l'analyse complète et [README.md](ocr-bench/README.md) le
protocole. `ocr-bench/results/report.html` affiche chaque page à côté de la sortie de chaque moteur.

## Tests

```powershell
uv run tests/test_scan_book.py          # ni scanner ni GPU requis
uv run tests/test_scan_book_deeplab.py
uv run tests/test_scan_book_qwen.py
uv run tests/test_ocr_bench.py
```

Définir `SCAN_BOOK_SKIP_GUI=1` pour ignorer les tests Tkinter sur une machine sans bureau.

## Dépannage

Si `devices` ne trouve aucun scanner : allumer le scanner, vérifier le câble USB, fermer les autres logiciels de
numérisation et contrôler le service WIA avec `Get-Service stisvc`.

Si Tkinter signale `Can't find a usable init.tcl`, lancer depuis un terminal Windows normal ou ajouter
`--python 3.12` à `uv run`.

Si les accents s'affichent mal dans PowerShell 5, lire les fichiers avec `Get-Content -Encoding UTF8`.
