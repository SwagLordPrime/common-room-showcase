"""One fixed-purpose child: count stdin text, write JSON to stdout, no file or network API."""
import hashlib,json,sys

def main():
    raw=sys.stdin.buffer.read(16385)
    if len(raw)>16384:raise ValueError('Input too large')
    data=json.loads(raw.decode('utf-8'))
    if type(data) is not dict or set(data)!={'body'} or type(data['body']) is not str or len(data['body'])>4000:
        raise ValueError('Invalid fixture input')
    body=data['body']
    result={'words':len(body.split()),'characters':len(body),'input_sha256':hashlib.sha256(body.encode('utf-8')).hexdigest()}
    sys.stdout.buffer.write(json.dumps(result,sort_keys=True).encode('utf-8'))
if __name__=='__main__':main()
