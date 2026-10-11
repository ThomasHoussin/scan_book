Consigne des sous-agents Opus qui rédigent la référence d'une page du jeu A (`Agent(model="opus")`).
Les champs `{...}` viennent de manifest.json.

---

Rédige la transcription de référence de la page `{id}` d'un livre français scanné. Elle servira à mesurer
le taux d'erreur de trois moteurs OCR : elle doit être exacte au caractère près.

Sources :
- Tuiles à pleine résolution (300 dpi, avec recouvrement), qui font foi :
  `{bench}/inputs/tiles/{id}/r1c1.png` … `r3c2.png` (r = rangée de haut en bas, c = colonne gauche/droite).
- Vue d'ensemble réduite, pour l'ordre de lecture : `{bench}/{llm_image}`.
- Trois transcriptions automatiques, seulement pour repérer les passages douteux :
  `{bench}/runs/marker2/{id}.txt`, `{bench}/runs/haiku/{id}.txt`, `{bench}/runs/luna/{id}.txt`.
  Ne les recopie pas : chaque mot doit être vérifié sur les tuiles. Là où elles divergent, tranche
  en regardant la tuile, jamais par vote.

Règles de transcription :
- Tout le texte imprimé, dans l'ordre de lecture (colonne de gauche entière, puis colonne de droite).
  Titres, intertitres, encadrés, tableaux de caractéristiques, légendes et notes compris.
- Une ligne imprimée = une ligne ; ligne vide entre les paragraphes ; césures de fin de ligne conservées.
- Orthographe, accents, majuscules et ponctuation exactement comme imprimés (fautes comprises).
- Texte brut, sans Markdown. Pour un tableau, une ligne par rangée, cellules séparées par des espaces.
- Une ou deux lettres masquées (reliure, tache) : écris le mot complet le plus probable, sans marque.
- N'écris PAS l'en-tête courant (titre du livre/chapitre répété en haut de page) ni le numéro de page.

Écris avec l'outil Write :
1. `{bench}/reference/{id}.txt` : la transcription.
2. `{bench}/reference/{id}.furniture.txt` : l'en-tête courant et le numéro de page, exactement comme imprimés,
   un par ligne (fichier vide s'il n'y en a pas).

Réponds ensuite en une ou deux phrases : difficultés rencontrées, passages où les trois transcriptions
se trompaient toutes.
