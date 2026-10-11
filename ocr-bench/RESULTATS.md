# Résultats du banc OCR (11/10/2026)

Protocole : [README.md](README.md). Détail page par page : `results/report.html`. Chiffres bruts : `results/metrics.json`.

## Synthèse

| | Marker 2 (auto) | Marker 2 (force_ocr) | Claude Haiku 5.5 | GPT-6 Luna |
|---|---|---|---|---|
| **CER, nos scans (16 p.)** | 14,94 % | 0,50 % | **0,25 %** | 0,50 % |
| CER hors *Les Figures* (15 p.) | 0,48 % | 0,48 % | **0,17 %** | 0,32 % |
| WER médian | 0,8 % | 0,8 % | **0,1 %** | 0,4 % |
| Pages vides | **1** (figures-0098) | 0 | 0 | 0 |
| **olmOCR-bench (20 p., 128 tests)** | 71,5 % | 71,5 % | **82,4 %** | 76,8 % |
| · vieux scans manuscrits (21 tests) | 14,3 % | 14,3 % | **47,6 %** | 38,1 % |
| · tableaux (20) | 90 % | 90 % | **100 %** | 90 % |
| · multi-colonnes (18) | 83,3 % | 83,3 % | 83,3 % | 83,3 % |
| · petits caractères (69) | 98,6 % | 98,6 % | 98,6 % | 95,7 % |
| Temps médian par page | 20,6 s | 18,0 s | 37,8 s* | 17,3 s |

\* Haiku passe par un sous-agent Claude Code : la durée et les ~37 000 tokens par page comprennent le
prompt système de l'agent ; un appel API direct serait bien plus léger. Luna : ~3 000 tokens d'entrée et
~2 700 de sortie par page (raisonnement compris). Marker : GPU local, plus 4 à 6 min de démarrage du conteneur vLLM.

## Ce qu'il faut retenir

1. **Sur des pages de livre propres, les trois moteurs sont très bons.** Moins de 0,5 % d'erreurs de
   caractères hors cas difficile, une bonne partie étant des détails (casse, apostrophes droites ou courbes déjà
   neutralisées, coquilles de l'imprimé que les moteurs corrigent). Pour un index de recherche, l'écart est faible.

2. **Le mode auto de Marker 2 peut rendre une page vide sans erreur.** Sur *Les Figures* p. 98 (police
   manuscrite stylisée), la mise en page détecte 22 blocs de texte, mais l'OCR qui suit ne renvoie rien. C'est le
   pire cas pour un index : la page devient introuvable sans aucun signal.
   **Avec `force_ocr`, même page : 0,60 % d'erreurs (meilleur score des quatre), et les 35 autres pages sont
   identiques au mode auto** : pas de texte parasite tiré des illustrations, rien à perdre à forcer l'OCR sur
   nos scans. Pour `scan_book` : `force_ocr=True`, ou au minimum repasser en `force_ocr` toute page vide.

3. **Haiku est le plus fidèle**, sur nos scans comme sur la vérité terrain humaine d'olmOCR, avec un net
   avantage sur le manuscrit et les tableaux. Il reste le plus lent ici (surcoût de l'agent).

4. **Luna corrige le texte.** Sur plusieurs pages, il « répare » les coquilles de l'imprimé (« joyeuseument »,
   « d’amor », « Il y a cela », « berseker »), remplace « oeuvre » par « œuvre » et invente parfois des noms propres
   proches (« Bedaryides », « Eyorstarbe » sur lion-vert-0050). Le modèle refuse `temperature=0` : sorties non
   déterministes.

5. **Marker 2 ne sait pas lire le manuscrit** (14 % sur les vieux scans olmOCR, contre 38 à 48 % pour les LLM).
   Il recompose les paragraphes (perte des fins de ligne et des césures, sans effet sur l'index), ajoute du
   Markdown, et commet quelques fautes d'accord (« dominées » pour « dominés ») ou de mots coupés à la reliure.

6. **Marker 2 est un changement d'architecture** par rapport à Marker 1 : l'OCR passe par un modèle
   vision-langage (`datalab-to/surya-ocr-2`) servi par vLLM dans Docker (image de plus de 10 Go, environ 85 % de la VRAM
   réservés). En prod, `scan_book` dépendrait de Docker Desktop et d'un GPU NVIDIA.

## Limites

- **Référence du jeu A rédigée par un LLM (Opus), sans relecture humaine.** L'agent voyait les sorties des
  moteurs pour repérer les désaccords ; sa mise en lignes ressemble à celle de Haiku. Le jeu B (vérité terrain
  humaine) donne cependant le même classement : Haiku > Luna > Marker.
- **Petits effectifs** : 16 + 20 pages. Les écarts sous ~0,3 point de CER, et les catégories olmOCR à 18-21 tests
  (une page vaut 2 à 6 tests), ne sont pas significatifs.
- **Entrées différentes** : Marker voit la page en pleine résolution, les LLM une version réduite à 2000 px.
- **En-têtes courants et numéros de page exclus du score** (choix voulu pour l'indexation) ; Marker les retire
  de lui-même.
- La page *Les Figures* p. 98 a été ajoutée à la main parce qu'elle était connue comme difficile : elle pèse
  lourd dans le CER global (7 100 caractères, environ 15 % du jeu A).
