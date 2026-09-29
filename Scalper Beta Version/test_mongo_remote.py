import paramiko

def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect('62.72.59.120', username='root', password='Root@#1234567', timeout=15)
    
    cmd = '''
    /root/astro_scalper_3005/venv/bin/python -c "
import certifi
from pymongo import MongoClient

uri = 'mongodb+srv://crestviewcorporate_db_user:Crestviewcorporate@cluster0.zfk4ahy.mongodb.net/?appName=Cluster0'
print('Testing with certifi:', certifi.where())
try:
    client = MongoClient(uri, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=5000)
    print('Databases:', client.list_database_names())
    scalper_db = client['Scalper']
    print('Scalper collections:', scalper_db.list_collection_names())
    for col in scalper_db.list_collection_names():
        cnt = scalper_db[col].count_documents({})
        print(f'  {col}: {cnt} documents')
        if cnt > 0:
            sample = scalper_db[col].find_one()
            print('    sample:', sample)
except Exception as e:
    print('Certifi attempt error:', e)

print('Testing with tlsAllowInvalidCertificates=True:')
try:
    client2 = MongoClient(uri, tlsAllowInvalidCertificates=True, serverSelectionTimeoutMS=5000)
    print('Databases2:', client2.list_database_names())
    scalper_db2 = client2['Scalper']
    print('Scalper collections2:', scalper_db2.list_collection_names())
    for col in scalper_db2.list_collection_names():
        cnt = scalper_db2[col].count_documents({})
        print(f'  {col}: {cnt} documents')
        if cnt > 0:
            sample = scalper_db2[col].find_one()
            print('    sample:', sample)
except Exception as e:
    print('Invalid certs error:', e)
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
