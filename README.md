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

## Trois variantes de recadrage

Les trois scripts ont les mêmes commandes et les mêmes touches. Ils partagent les sessions, les dossiers, le PDF et
le verrou. On peut donc passer de l'un à l'autre en cours de livre :
1. attendre la fin de l'acquisition ;
2. quitter avec **Q** ;
3. relancer le même livre avec l'autre script.

Ils ne diffèrent que par la façon de trouver le cadre de la page. La remise à l'endroit et le redressement sont
communs.

| | `scan_book.py` (original) | `scan_book_deeplab.py` | `scan_book_qwen.py` |
|---|---|---|---|
| Recadrage | Heuristiques d'image (contour du papier, ombre de bord, fond du scanner) | Segmentation DeepLabV3-ResNet50 (poids [LearnOpenCV](https://github.com/spmallick/learnopencv/tree/master/Document-Scanner-Custom-Semantic-Segmentation-using-PyTorch-DeepLabV3)) | Qwen3.5 9B en local via [Ollama](https://ollama.com), qui renvoie un rectangle |
| Marge conservée | 3 mm | 0,5 mm | 0,5 mm |
| Garde-fou | Limite incertaine conservée | Découpe rejetée si elle coupe du texte détecté | Réponse incertaine ou zone < 20 % rejetée |
| Dépendances | Aucune lourde | PyTorch CUDA (~3 Go), poids de 161 Mo téléchargés dans `.cache/` | Ollama démarré, modèle `qwen3.5:9b` (~6,6 Go) |
| Vitesse | Immédiat | Rapide sur GPU | 2,5 à 3 s par page une fois le modèle chargé |
| Déroulé | Chaque page est traitée et affichée avant la suivante | Idem | Scan en masse, traitement en tâche de fond, puis vérification page par page avant l'export |

Tout le traitement reste local : aucune image n'est envoyée à un service distant.

Bilan des essais, faits sur 4 pages seulement :
- **DeepLab** confond parfois la page avec ses blocs imprimés. Ses découpes sont souvent rejetées par le garde-fou,
  ce qui laisse la page entière à recadrer à la main. Ce n'est pas une amélioration générale.
- **Qwen** recadre correctement les pages intérieures, mais ses coordonnées restent approximatives, et le cadre est
  parfois serré près des titres. Vérifier l'aperçu.

Dans le parcours Qwen :
- **Entrée** valide une page en vérification et **C** la corrige.
- **F** n'exporte qu'une fois toutes les pages validées.
- Une page dont l'image a été supprimée dans `pages/` est ignorée à l'export.
- **Une session Qwen en cours doit être reprise avec le script Qwen.** Les autres scripts ne connaissent pas ses
  pages en attente.

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

Les PDF produits n'ont pas de couche texte. `ocr-bench/` compare trois moteurs pour en ajouter une :
- **Marker 2**, local sur GPU ;
- **Claude Haiku 5.5** ;
- **GPT-6 Luna**, via AWS Bedrock.

Deux jeux de pages servent au test :
- **16 pages de nos scans**, notées contre une référence ;
- **20 pages d'[olmOCR-bench](https://huggingface.co/datasets/allenai/olmOCR-bench)**, notées sur leurs extraits
  vérifiés par des humains.

Le score est le taux d'erreur par caractère (CER), plus bas = meilleur :

| | Marker 2 (force_ocr) | Claude Haiku 5.5 | GPT-6 Luna |
|---|---|---|---|
| Nos scans (hors page difficile) | 0,72 % | **0,35 %** | 0,37 % |
| Page en police manuscrite stylisée | 1,1 % (vide en mode auto) | **0,75 %** | 1,6 % |
| olmOCR, extraits | 1,0 % | **0,46 %** | 0,80 % |
| Temps médian par page | 18 s | ≈ 38 s (via agent) | 17 s |

Ce qu'il faut retenir :
- **Haiku est retenu** : le plus fidèle, il garde les titres de page et de chapitre, et ne demande aucune infrastructure.
- **Marker 2 en mode auto peut rendre une page vide sans erreur.** Il supprime aussi les titres, même avec `force_ocr`.
  Enfin, il passe par un serveur vLLM dans Docker.
- **Luna corrige les coquilles de l'imprimé** et n'accepte pas `temperature=0`.

Documents du banc :
- **[ocr-bench/SYNTHESE.md](ocr-bench/SYNTHESE.md)** : paramètres réutilisables (taille d'image, consigne, contrôles) ;
- **[ocr-bench/RESULTATS.md](ocr-bench/RESULTATS.md)** : analyse complète ;
- **[ocr-bench/README.md](ocr-bench/README.md)** : protocole ;
- **`ocr-bench/results/report.html`** : comparaison visuelle, page par page.

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
