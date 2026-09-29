import paramiko

def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect('62.72.59.120', username='root', password='Root@#1234567', timeout=15)
    
    cmd = '''
    /root/astro_scalper_3005/venv/bin/python -c "
from pymongo import MongoClient

uris = [
    'mongodb://crestviewcorporate_db_user:Crestviewcorporate@ac-kjmgfzp-shard-00-00.zfk4ahy.mongodb.net:27017/?ssl=true&authSource=admin&directConnection=true',
    'mongodb://crestviewcorporate_db_user:Crestviewcorporate@ac-kjmgfzp-shard-00-01.zfk4ahy.mongodb.net:27017/?ssl=true&authSource=admin&directConnection=true',
    'mongodb://crestviewcorporate_db_user:Crestviewcorporate@ac-kjmgfzp-shard-00-00.zfk4ahy.mongodb.net:27017,ac-kjmgfzp-shard-00-01.zfk4ahy.mongodb.net:27017/?ssl=true&replicaSet=atlas-kjmgfzp-shard-0&readPreference=secondaryPreferred&authSource=admin'
]

for u in uris:
    print('Testing uri:', u[:60])
    try:
        client = MongoClient(u, serverSelectionTimeoutMS=4000)
        print('  Databases:', client.list_database_names())
        scalper_db = client['Scalper']
        print('  Scalper collections:', scalper_db.list_collection_names())
        for col in scalper_db.list_collection_names():
            cnt = scalper_db[col].count_documents({})
            print(f'    {col}: {cnt} documents')
            if cnt > 0:
                sample = list(scalper_db[col].find().limit(3))
                for t in sample:
                    print('      Trade:', t.get('trade_id'), t.get('contract_symbol'), t.get('pnl_rupees'), t.get('exit_time'))
    except Exception as e:
        print('  Error:', e)
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
