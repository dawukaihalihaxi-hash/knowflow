import os
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'src'))
from web_app import create_app
config=os.environ.get('APP_CONFIG')
if not config:raise RuntimeError('请设置APP_CONFIG，指向项目配置文件。')
app=create_app(ROOT/config)
if __name__=='__main__':app.run(host='127.0.0.1',port=int(os.environ.get('PORT','8085')),debug=False,threaded=True)
