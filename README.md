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