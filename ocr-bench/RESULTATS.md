# Résultats du banc OCR (11/10/2026)

Protocole : [README.md](README.md). Détail page par page : `results/report.html`. Chiffres bruts : `results/metrics.json`.

## Synthèse

| | Marker 2 (auto) | Marker 2 (force_ocr) | Claude Haiku 5.5 | GPT-6 Luna |
|---|---|---|---|---|
| **CER, nos scans (16 p.)** | 15,14 % | 0,78 % | **0,41 %** | 0,55 % |
| CER hors *Les Figures* (15 p.) | 0,72 % | 0,72 % | **0,35 %** | 0,37 % |
| WER médian | 1,3 % | 1,3 % | **0,2 %** | 0,4 % |
| Pages vides | **1** (figures-0098) | 0 | 0 | 0 |
| **CER olmOCR, extraits (20 p.)** | 1,01 % | 1,07 % | **0,46 %** | 0,80 % |
| · vieux scans manuscrits (1 640 car.) | 3,60 % | 3,84 % | **1,65 %** | 2,74 % |
| · tableaux, contenu des cellules (300 car.) | 0 % | 0 % | 0 % | 0 % |
| · multi-colonnes (3 300 car.) | 0,27 % | 0,27 % | **0,06 %** | 0,09 % |
| · petits caractères (5 680 car.) | 0,16 % | 0,16 % | **0,14 %** | 0,37 % |
| *Tests officiels olmOCR, pour mémoire* | *71,5 %* | *71,5 %* | *82,4 %* | *76,8 %* |
| Temps médian par page | 20,6 s | 18,0 s | 37,8 s* | 17,3 s |

CER olmOCR : même calcul que sur nos scans, limité aux extraits de texte que fournit olmOCR (environ 15 % du
texte des pages, olmOCR ne donnant pas de transcription complète). Les tests officiels, en réussi/échoué strict
(une phrase lue à 97 % compte comme absente), sont gardés à titre indicatif.

\* Haiku passe par un sous-agent Claude Code : la durée et les ~37 000 tokens par page comprennent le
prompt système de l'agent ; un appel API direct serait bien plus léger. Luna : ~3 000 tokens d'entrée et
~2 700 de sortie par page (raisonnement compris). Marker : GPU local, plus 4 à 6 min de démarrage du conteneur vLLM.

## Ce qu'il faut retenir

1. **Sur des pages de livre propres, les trois moteurs sont très bons.** Moins de 1 % d'erreurs de
   caractères hors cas difficile, une bonne partie étant des détails (casse, apostrophes droites ou courbes déjà
   neutralisées, coquilles de l'imprimé que les moteurs corrigent). Pour un index de recherche, l'écart est faible.

2. **Le mode auto de Marker 2 peut rendre une page vide sans erreur.** Sur *Les Figures* p. 98 (police
   manuscrite stylisée), la mise en page détecte 22 blocs de texte, mais l'OCR qui suit ne renvoie rien. C'est le
   pire cas pour un index : la page devient introuvable sans aucun signal.
   **Avec `force_ocr`, même page : 1,1 % d'erreurs, et les 35 autres pages sont
   identiques au mode auto** : pas de texte parasite tiré des illustrations, rien à perdre à forcer l'OCR sur
   nos scans. Pour `scan_book` : `force_ocr=True`, ou au minimum repasser en `force_ocr` toute page vide.

3. **Marker 2 supprime les titres de page, même en `force_ocr`.** Il classe en « en-tête » et retire le titre de
   la lettre (« Les confidences du Duc de Saint-Amand » : on ne sait plus qui parle), le chapitre du Livre Noir
   (« La Kabbale Noire », « La Pavane », « La Nécromancie » : on ne sait plus à quelle école appartient un sort) et le
   bandeau de section du Lion Vert (« L’Incunable Souveraineté »). Les LLM les gardent. Pour l'index, c'est un trou
   de contexte plus grave que quelques fautes de lettres ; il faudrait désactiver ce filtrage dans Marker (non testé).

4. **Haiku est le plus fidèle**, de peu devant Luna sur nos scans (0,35 % contre 0,37 % hors *Les Figures*, écart
   non significatif), et sur la vérité terrain humaine d'olmOCR (0,46 % contre 0,80 % pour Luna et environ 1 % pour Marker),
   surtout grâce au manuscrit. Il reste le plus lent ici (surcoût de l'agent).

5. **Luna corrige le texte.** Sur plusieurs pages, il « répare » les coquilles de l'imprimé (« joyeuseument »,
   « d’amor », « Il y a cela », « berseker »), remplace « oeuvre » par « œuvre » et invente parfois des noms propres
   proches (« Bedaryides », « Eyorstarbe » sur lion-vert-0050). Le modèle refuse `temperature=0` : sorties non
   déterministes.

6. **Le manuscrit reste le point faible de tous**, Marker 2 en tête : 3,6 % d'erreurs sur les lettres
   manuscrites olmOCR, contre 1,7 % pour Haiku et 2,7 % pour Luna, souvent sur des majuscules ambiguës. Les tableaux ne
   départagent personne sur le contenu des cellules ; seule la notation officielle (structure) y voit des écarts.
   Marker 2 recompose les paragraphes (perte des fins de ligne et des césures, sans effet sur l'index), ajoute du
   Markdown, et commet quelques fautes d'accord (« dominées » pour « dominés ») ou de mots coupés à la reliure.

7. **Marker 2 est un changement d'architecture** par rapport à Marker 1 : l'OCR passe par un modèle
   vision-langage (`datalab-to/surya-ocr-2`) servi par vLLM dans Docker (image de plus de 10 Go, environ 85 % de la VRAM
   réservés). En prod, `scan_book` dépendrait de Docker Desktop et d'un GPU NVIDIA.

## Limites

- **Référence du jeu A rédigée par un LLM (Opus), sans relecture humaine.** L'agent voyait les sorties des
  moteurs pour repérer les désaccords ; sa mise en lignes ressemble à celle de Haiku. Le jeu B (vérité terrain
  humaine, sur extraits) donne cependant le même classement : Haiku > Luna > Marker.
- **Petits effectifs** : 16 + 20 pages, et seulement ~11 000 caractères d'extraits côté olmOCR. Les écarts sous
  ~0,3 point de CER ne sont pas significatifs.
- **Entrées différentes** : Marker voit la page en pleine résolution, les LLM une version réduite à 2000 px.
- **Titre du livre répété en en-tête et numéros de page exclus du score** (inutiles pour l'index). Les titres de
  chapitre, de section ou de lettre sont gardés et comptés.
- La page *Les Figures* p. 98 a été ajoutée à la main parce qu'elle était connue comme difficile : elle pèse
  lourd dans le CER global (7 100 caractères, environ 15 % du jeu A).
