# Runtime

http://127.0.0.1:5000/

# Setup

### Install requirements
```
pip3 install -r requirements.txt
```

### Run from cli
```
python3 app.py
```

### Pack to Windows application
```
python3 -m PyInstaller --onefile --add-data "templates:templates" app.py
```
## Docker и локальные стили

Compose публикует сервис на http://localhost:5081/ (`5081:5000`).
Bootstrap 5.3.2 поставляется в `static/bootstrap.min.css`, лицензия MIT —
`static/bootstrap-LICENSE.txt`; форма загружает CSS с самого приложения.
