# Banc OCR : Marker 2 / Claude Haiku 5.5 / GPT-6 Luna

Mesure de la qualité de transcription de trois moteurs avant d'ajouter une couche texte aux PDF de `scan_book`.
Résultats : [RESULTATS.md](RESULTATS.md). Visualisation page par page : `results/report.html` (ouvrir dans un navigateur ; ← → pour naviguer).

## Moteurs

| Moteur | Exécution | Entrée |
|---|---|---|
| Marker 2 (`marker-pdf==2.0.0`, surya-ocr 0.22) | local, RTX 5070 Ti ; surya 2 lance un conteneur Docker vLLM (`datalab-to/surya-ocr-2`) | PNG pleine résolution |
| Claude Haiku 5.5 | un sous-agent Claude Code (`model: haiku`) par page, outil Read sur l'image | PNG réduit (bord long 2000 px) |
| GPT-6 Luna | AWS Bedrock Converse, `us.openai.gpt-6-luna`, us-east-1 (température imposée par le modèle) | PNG réduit (bord long 2000 px) |

Marker tourne en mode auto (pas de `force_ocr`) : les entrées sont des images, il fait donc de l'OCR de toute façon.

## Jeux de pages

- **A, nos scans (16 pages)** : 5 pages intérieures tirées au hasard (graine 20261011) dans chacun des livres
  Les Veilleurs, Le Lion Vert, Le Livre Noir, plus la page 98 du PDF *Les Figures* (police manuscrite stylisée), ajoutée à la main.
  Référence rédigée par un sous-agent Opus à partir de tuiles pleine résolution, **sans relecture humaine**.
- **B, complément olmOCR-bench (20 pages)** : 5 pages tirées au hasard dans chacune des catégories `old_scans`,
  `table_tests`, `multi_column`, `long_tiny_text` d'[allenai/olmOCR-bench](https://huggingface.co/datasets/allenai/olmOCR-bench),
  dont la vérité terrain est vérifiée par des humains (128 tests).

## Consignes

- `prompt.md` (jeu A) : transcription fidèle ligne par ligne, texte brut ; une ou deux lettres abîmées complétées
  **sans marque** (sortie directement indexable) ; `[illisible]` seulement pour un passage entier.
- `prompt_olmocr.md` (jeu B) : Markdown, tableaux en Markdown/HTML, en-têtes et pieds de page omis.
- `haiku_agent_prompt.md` : la même consigne + chemins d'entrée/sortie. Aucune consigne ne mentionne le banc d'essai.
- `reference_prompt.md` : consigne des agents Opus qui rédigent les références.

## Scores

- **Jeu A** : CER et WER (rapidfuzz) après normalisation identique pour tous (NFC, apostrophes/guillemets, ligatures,
  œ→oe, traits d'union entre lettres retirés, Markdown/HTML retiré, espaces). Seuls le titre du livre répété en
  en-tête et le numéro de page (`reference/<id>.furniture.txt`) sont **exclus du score**, car ils sont inutiles pour l'index.
  Les titres de chapitre, de section ou de lettre (qui parle, quelle école de magie…) sont gardés et comptés. Indicateurs annexes : erreurs
  d'accent, mots ajoutés, blocs omis (≥ 8 mots).
- **Jeu B** : CER calculé comme pour le jeu A, mais limité aux extraits de texte que contiennent les tests olmOCR
  (phrases attendues, textes d'ordre de lecture, cellules de tableau ; environ 15 % du texte des pages). Pour chaque
  extrait, on retient le passage le plus proche dans la sortie du moteur. Moyenne des catégories. Le taux de tests
  officiels réussis (`olmocr.bench.tests`, réussi/échoué strict) est gardé pour mémoire ; une sortie vide y échoue à tout.

## Reproduire

```
uv run ocr-bench/prepare.py            # tirage figé dans manifest.json (--force pour le refaire)
uv run ocr-bench/prepare.py --add-pdf <pdf> <page> <id> <livre>   # page ajoutée à la main
uv run ocr-bench/make_tiles.py         # tuiles 300 dpi pour les références
uv run ocr-bench/run_marker2.py        # nécessite Docker Desktop + GPU NVIDIA
uv run ocr-bench/run_luna.py           # identifiants AWS (profil default)
# Haiku : un Agent(model="haiku") par page avec haiku_agent_prompt.md, puis
uv run ocr-bench/haiku_meta.py <id> <secondes> <tokens>
uv run ocr-bench/score.py              # results/metrics.json + results/report.html
uv run tests/test_ocr_bench.py
```

`inputs/` (images, PDF olmOCR, tuiles) est gitignoré ; tout le reste est versionné.
