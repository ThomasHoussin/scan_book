Consigne envoyée à chaque sous-agent Haiku (`Agent(model="haiku")`), une page par agent.
Les champs `{...}` viennent de manifest.json.

---

{prompt}

Image de la page : `{bench}/{llm_image}` (lis-la avec l'outil Read).
Écris ta transcription, et rien d'autre, dans `{bench}/runs/haiku/{id}{ext}` avec l'outil Write, puis réponds « OK ».
N'utilise pas d'autre outil et ne lis aucun autre fichier.
