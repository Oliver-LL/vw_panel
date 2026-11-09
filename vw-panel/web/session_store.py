"""Persist bounded, password-bound sessions across panel restarts."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time


class SessionStore:
    def __init__(self, state, ttl=43200, maximum=32):
        self.state=Path(state);self.path=self.state/'sessions.json';self.ttl=ttl;self.maximum=maximum
        self.sessions={};self.binding=self.credential_binding()
        try:
            if self.path.stat().st_size>65536:return
            data=json.loads(self.path.read_text())
            if data.get('version')!=1 or data.get('credentials')!=self.binding:return
            now=time.time()
            for key,item in data.get('sessions',{}).items():
                if len(self.sessions)>=maximum:break
                if not re.fullmatch(r'[a-f0-9]{64}',key):continue
                csrf,created=item
                if not isinstance(csrf,str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',csrf):continue
                if not isinstance(created,(int,float)) or not 0<=now-created<ttl:continue
                self.sessions[key]=(csrf,created)
        except (OSError,ValueError,TypeError,AttributeError):self.sessions.clear()

    def credential_binding(self):
        try:
            value=json.loads((self.state/'credentials.json').read_text())
            raw=json.dumps({k:value[k] for k in ('salt','hash')},sort_keys=True).encode()
            return hashlib.sha256(raw).hexdigest()
        except (OSError,ValueError,KeyError,TypeError):return None

    @staticmethod
    def key(token):return hashlib.sha256(token.encode()).hexdigest()

    def persist(self):
        self.state.mkdir(parents=True,exist_ok=True,mode=0o700)
        temporary=self.path.with_name('sessions.'+secrets.token_hex(8)+'.tmp')
        try:
            fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(fd,'w') as stream:
                json.dump({'version':1,'credentials':self.binding,'sessions':self.sessions},stream)
                stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,self.path)
        except OSError:
            raise ValueError('无法保存登录状态，请检查面板状态目录权限。') from None
        finally:temporary.unlink(missing_ok=True)

    def sync_credentials(self):
        binding=self.credential_binding()
        if binding!=self.binding:
            self.sessions.clear();self.binding=binding;self.persist()
        return binding is not None

    def create(self):
        if not self.sync_credentials():raise ValueError('管理员凭据不可读取，请修复配置后重试。')
        now=time.time()
        for key,(_,created) in list(self.sessions.items()):
            if not 0<=now-created<self.ttl:self.sessions.pop(key,None)
        if len(self.sessions)>=self.maximum:
            self.sessions.pop(min(self.sessions,key=lambda key:self.sessions[key][1]),None)
        token,csrf=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
        key=self.key(token);self.sessions[key]=(csrf,now)
        try:self.persist()
        except ValueError:self.sessions.pop(key,None);raise
        return token,csrf

    def auth(self,token):
        if not isinstance(token,str) or len(token)>1024 or not self.sync_credentials():return None
        key=self.key(token);value=self.sessions.get(key)
        if not value:return None
        csrf,created=value
        if not 0<=time.time()-created<self.ttl:
            self.sessions.pop(key,None);self.persist();return None
        return csrf

    def revoke(self,token):
        self.sessions.pop(self.key(token),None);self.persist()
