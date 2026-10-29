"""VM-scoped package-download proxy; no Host forwarding/firewall changes."""
import argparse
import json
from pathlib import Path
import select
import socket
import socketserver
import time
from urllib.parse import urlsplit

ALLOWED={'pypi.org','pypi.python.org','files.pythonhosted.org','repo.anaconda.com','conda.anaconda.org'}

class Proxy(socketserver.StreamRequestHandler):
    def handle(self):
        started=time.time();peer=None;host='';counts=[0,0];error=None
        try:
            line=self.rfile.readline(65536).decode('iso-8859-1').strip()
            method,target,version=line.split(' ',2)
            headers=[]
            while True:
                line=self.rfile.readline(65536)
                if not line or line in [b'\r\n',b'\n']:break
                headers.append(line)
            if method=='CONNECT':
                host,port=target.rsplit(':',1);port=int(port)
            else:
                parsed=urlsplit(target);host=parsed.hostname;port=parsed.port or 80
                if method not in ['GET','HEAD']:raise ValueError('Only package reads are allowed')
            if host not in ALLOWED or port not in [80,443]:raise ValueError('Package endpoint is outside the recorded dependency scope')
            peer=socket.create_connection((host,port),timeout=30)
            if method=='CONNECT':self.wfile.write(b'HTTP/1.1 200 Connection Established\r\n\r\n');self.wfile.flush()
            else:
                path=parsed.path or '/'
                if parsed.query:path+='?'+parsed.query
                clean=[v for v in headers if not v.lower().startswith((b'proxy-connection:',b'proxy-authorization:'))]
                peer.sendall((method+' '+path+' '+version+'\r\n').encode()+b''.join(clean)+b'Connection: close\r\n\r\n')
            channels=[self.connection,peer]
            while True:
                ready,_,_=select.select(channels,[],[],90)
                if not ready:break
                for source in ready:
                    block=source.recv(65536)
                    if not block:return
                    dest=peer if source is self.connection else self.connection
                    dest.sendall(block);counts[0 if source is self.connection else 1]+=len(block)
        except Exception as exc:
            error=type(exc).__name__+': '+str(exc)
            try:self.wfile.write(b'HTTP/1.1 502 Package proxy unavailable\r\nConnection: close\r\n\r\n')
            except OSError:pass
        finally:
            if peer:peer.close()
            with self.server.log.open('a') as stream:
                stream.write(json.dumps(dict(epoch=time.time(),host=host,client_bytes=counts[0],server_bytes=counts[1],wall_s=time.time()-started,error=error))+'\n')

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address=True
    daemon_threads=True

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--bind',required=True);parser.add_argument('--port',type=int,required=True);parser.add_argument('--log',type=Path,required=True);args=parser.parse_args()
    if not args.bind.startswith('192.168.') or args.port<1024:raise ValueError('Use only the task VM gateway and an unprivileged port')
    with Server((args.bind,args.port),Proxy) as server:
        server.log=args.log;server.serve_forever()

if __name__=='__main__':main()
