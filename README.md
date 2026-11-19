# Pilote

TUI pour lancer depuis son poste un service vscode-python sur le SSPCloud, avec un dépôt Github cloné et l'agent Pi branché sur l'API Albert.

Dans SSPCloud :
- enregistrer son PAT Github 
- ajouter un secret `albert` contenant la variable `ALBERT_API_KEY` 

Puis :

```powershell
python pilote.py
```
ou


```powershell
scoop shim add pilote (Get-Command python).Source "$PWD\pilote.py"
pilote
```
