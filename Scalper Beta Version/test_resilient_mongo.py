import paramiko

def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect('62.72.59.120', username='root', password='Root@#1234567', timeout=15)
    
    cmd = '''
    /root/astro_scalper_3005/venv/bin/python -c "
from pymongo import MongoClient

candidates = [
    'mongodb://crestviewcorporate_db_user:Crestviewcorporate@ac-kjmgfzp-shard-00-00.zfk4ahy.mongodb.net:27017,ac-kjmgfzp-shard-00-01.zfk4ahy.mongodb.net:27017/Scalper?ssl=true&authSource=admin',
    'mongodb://crestviewcorporate_db_user:Crestviewcorporate@ac-kjmgfzp-shard-00-00.zfk4ahy.mongodb.net:27017/Scalper?ssl=true&authSource=admin&directConnection=true',
    'mongodb://crestviewcorporate_db_user:Crestviewcorporate@ac-kjmgfzp-shard-00-01.zfk4ahy.mongodb.net:27017/Scalper?ssl=true&authSource=admin&directConnection=true'
]

for uri in candidates:
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=2000)
        db = client['Scalper']
        u1_trades = list(db['User 1'].find().sort('_id', -1).limit(5))
        admin_trades = list(db['Admin'].find().sort('_id', -1).limit(5))
        print('SUCCESS with URI:', uri[:65])
        print(f'  User 1 recent count: {len(u1_trades)}, Admin recent count: {len(admin_trades)}')
        for t in u1_trades:
            print('    U1 Trade:', t.get('trade_id'), t.get('instrument'), t.get('contract_symbol'), t.get('pnl_rupees'), t.get('exit_time'))
        break
    except Exception as e:
        print('Failed URI:', uri[:65], e)
"
    '''
    stdin, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode('utf-8', errors='replace').encode('ascii', errors='replace').decode('ascii')
    err = stderr.read().decode('utf-8', errors='replace').encode('ascii', errors='replace').decode('ascii')
    print(out)
    if err:
        print('STDERR:', err)
    ssh.close()

if __name__ == '__main__':
    main()
