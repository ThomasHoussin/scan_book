# OCR de pages de livres scannés : paramètres à retenir

Synthèse réutilisable du banc `scan_book/ocr-bench` (11/10/2026) : 16 pages de livres de jeu de rôle français
scannés à 300 dpi, plus 20 pages olmOCR-bench. Détails : [RESULTATS.md](RESULTATS.md), [README.md](README.md).

## Choix retenu : Claude Haiku 5.5

- Le plus fidèle sur les deux jeux de pages, à égalité ou devant GPT-6 Luna, nettement devant Marker 2.
- Garde les titres de page, de chapitre et de lettre. Marker 2 les supprime, ce qui fait perdre « qui parle » ou
  « quel chapitre ».
- Aucune page vide ni tronquée sur 36 pages.
- Pas d'infrastructure à maintenir : pas de GPU, pas de Docker ni de vLLM.
- Identifiants du modèle :
  - API Anthropic : `claude-haiku-5-5`
  - AWS Bedrock : profils d'inférence `us.anthropic.claude-haiku-5-5` ou `global.anthropic.claude-haiku-5-5`

**Testé uniquement via des sous-agents Claude Code.** Ne sont donc pas encore vérifiés :
- l'appel API direct ;
- le mode batch ;
- le réglage de `temperature` ;
- le nombre de tokens réel par page.

## Paramètres

### Image d'entrée

- Source : page recadrée à 300 dpi, en PNG.
- Réduite pour le modèle :
  - bord long de **2000 px**, rééchantillonnage Lanczos ;
  - **niveaux de gris si la source l'est**, couleur sinon ;
  - PNG optimisé de **moins de 5 Mo** (en pratique 1 à 1,7 Mo).
- Une page par requête.
- Code de référence : `llm_image()` dans [prepare.py](prepare.py).

### Consigne (texte exact testé, [prompt.md](prompt.md))

```
Tu es un moteur OCR. Transcris intégralement le texte imprimé de cette page de livre en français.

Règles :
- Transcription fidèle, ligne par ligne, dans l'ordre de lecture. Une ligne imprimée = une ligne de sortie ; ligne vide entre les paragraphes.
- Conserve l'orthographe, les accents, la ponctuation, les majuscules et les césures de fin de ligne telles qu'imprimées.
- Texte brut uniquement : pas de Markdown, pas de balises, pas de commentaire, pas d'introduction ni de conclusion.
- Inclus titres, en-têtes, numéros de page, notes et légendes là où ils apparaissent.
- Si une ou deux lettres sont abîmées ou masquées (près de la reliure, tache, pli), écris directement le mot complet le plus probable, sans aucune marque.
- N'invente jamais un mot entier. Si un passage entier est illisible, écris [illisible] à sa place.
- Si la page ne contient aucun texte, ne renvoie rien.
```

Choix volontaires :
- **Lettres reconstituées sans marque** : le texte reste directement indexable.
- **Aucune mention d'un test dans la consigne** : le modèle se comporte comme en production.
- **Pas de Markdown** : la sortie reste propre pour un index.
- Pour une sortie structurée (tableaux, titres), voir la variante en anglais [prompt_olmocr.md](prompt_olmocr.md).

### Contrôles à prévoir en production

- **Sortie vide ou très courte** sur une page qui contient du texte : relancer.
  - C'est le défaut silencieux observé avec Marker 2.
  - Repère : longueur médiane d'une page de texte ≈ 3 000 caractères.
- **Arrêt sur la limite de tokens** (`stop_reason` ≠ fin normale) : relancer avec une limite plus haute.
  - En pratique ≈ 3 000 à 9 000 caractères par page, soit quelques milliers de tokens de sortie.
- **Compter les `[illisible]`** pour repérer les pages à revoir (il n'y en a eu aucune sur les 16 scans).

### Post-traitement pour l'indexation (optionnel)

À appliquer de la même façon partout, pour la recherche plutôt que pour l'affichage :
- forme NFC ;
- apostrophes et guillemets unifiés ;
- œ → oe ;
- ligatures dépliées ;
- recollage des césures de fin de ligne ;
- espaces normalisées.

Code : `normalize()` dans [score.py](score.py).

## Résultats clés

Taux d'erreur par caractère (CER) après normalisation. Pour olmOCR, il est calculé sur les extraits de vérité
terrain humaine, qui couvrent environ 15 % du texte.

| | Marker 2 (force_ocr) | **Haiku 5.5** | GPT-6 Luna |
|---|---|---|---|
| Scans de livres (15 p., hors page difficile) | 0,72 % | **0,35 %** | 0,37 % |
| Page en police manuscrite stylisée | 1,1 % (vide en mode auto) | **0,75 %** | 1,6 % |
| olmOCR, lettres manuscrites | 3,6 % | **1,7 %** | 2,7 % |
| olmOCR, multi-colonnes / petits caractères / tableaux | 0,27 / 0,16 / 0 % | **0,06 / 0,14 / 0 %** | 0,09 / 0,37 / 0 % |
| Temps médian par page | 18 s (GPU local) | ≈ 38 s via agent (non représentatif de l'API) | 17 s |

Les erreurs restantes de Haiku sont surtout :
- des apostrophes droites au lieu de courbes (sans effet après normalisation) ;
- des majuscules ambiguës dans le manuscrit ;
- quelques coquilles de l'imprimé qu'il corrige (« étonnament » → « étonnamment »).

## Ce qu'on sait des autres moteurs, si la question revient

- **Marker 2** (`marker-pdf` 2.0.0, surya-ocr 0.22) :
  - **Infrastructure** : l'OCR passe par un modèle vision-langage servi par vLLM dans Docker, sur GPU NVIDIA (image de plus de 10 Go, 85 % de la VRAM, 4 à 6 min de démarrage).
  - **Page vide** : en mode auto, il peut rendre une page vide sans erreur. Il faut `force_ocr=True`, qui ne coûte rien sur des scans.
  - **Titres et en-têtes supprimés**, même en `force_ocr`.
  - **Mise en forme** : il recompose les paragraphes et ajoute du Markdown.
- **GPT-6 Luna** (Bedrock `us.openai.gpt-6-luna`) :
  - refuse `temperature=0`, donc ses sorties ne sont pas déterministes ;
  - corrige les coquilles de l'imprimé ;
  - invente parfois des noms propres proches (« Bedaryides »).

## Limites de ces chiffres

- **Petits effectifs** : 16 + 20 pages. Les écarts de moins de 0,3 point ne sont pas significatifs.
- **Références des scans écrites par Opus, sans relecture humaine.** olmOCR (vérité terrain humaine) confirme le classement.
- **Exclus du score** : le titre du livre répété en en-tête et les numéros de page. Tous les autres titres comptent.
