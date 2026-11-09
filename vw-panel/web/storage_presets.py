"""Data-driven storage presets for the remote storage form and save action."""
import re
from pathlib import Path
from urllib.parse import urlsplit


def _field(key, label, **kwargs):
    return {'key': key, 'label': label, **kwargs}


BUCKET_PATTERN = r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]'
BUCKET_HELP = '只填桶名，不包含路径或 endpoint。'
WEBDAV_VENDORS = [
    {'value': 'other', 'label': '通用 WebDAV'},
    {'value': 'nextcloud', 'label': 'Nextcloud'},
    {'value': 'owncloud', 'label': 'ownCloud'},
    {'value': 'sharepoint', 'label': 'SharePoint'},
    {'value': 'sharepoint-ntlm', 'label': 'SharePoint (NTLM)'},
]
S3_PROVIDERS = [
    {'value': 'AWS', 'label': 'Amazon AWS'},
    {'value': 'Minio', 'label': 'MinIO'},
    {'value': 'Backblaze', 'label': 'Backblaze B2'},
    {'value': 'Wasabi', 'label': 'Wasabi'},
    {'value': 'Alibaba', 'label': '阿里云 OSS'},
    {'value': 'Other', 'label': '其他 S3 兼容'},
]

PRESETS = {
    'r2': {
        'label': 'Cloudflare R2',
        'type': 's3',
        'description': '填写 R2 API Token 与 Bucket，保存前用只读列表验证；Token 需具备 Object Read & Write 权限。',
        'requires_provider': 'Cloudflare',
        'fields': [
            _field('account_id', 'Account ID', config=False, maxlength=32, placeholder='32 位 Account ID',
                   help='首次配置填写；重新配置已有 R2 时留空会沿用原 endpoint。'),
            _field('jurisdiction', '数据区域', type='select', config=False, default='',
                   options=[{'value': '', 'label': '默认区域'}, {'value': 'eu', 'label': '欧盟 (EU)'},
                            {'value': 'fedramp', 'label': 'FedRAMP'}],
                   allow_custom=True, pattern=r'[a-z0-9-]{1,32}',
                   help='仅当 Bucket 创建在非默认区域时选择。'),
            _field('access_key_id', 'Access Key ID', maxlength=128, reuse_blank=True,
                   pattern=r'[A-Za-z0-9]{8,128}', pattern_message='Access Key ID 格式不正确。',
                   help='R2 API Token 的 Access Key ID；已有配置可留空保留。'),
            _field('secret_access_key', 'Secret Access Key', type='password', secret=True, reuse_blank=True,
                   help='R2 API Token 的 Secret；留空保留已保存的密钥。'),
            _field('bucket', 'Bucket', config=False, required=True, maxlength=63, pattern=BUCKET_PATTERN,
                   pattern_message='Bucket 名称需为 3～63 位小写字母、数字或短横线，并以字母或数字开头和结尾。',
                   help=BUCKET_HELP + '需先在 Cloudflare 创建。'),
            _field('path', '桶内保存路径', config=False, default='VaultBackup', maxlength=512,
                   help='可选；留空表示桶根目录。'),
            _field('no_check_bucket', '对象级 Token（no_check_bucket）', type='checkbox', default=True,
                   help='默认开启；对象级 Token 或无法读取桶信息时必须开启。Bucket 需已存在。'),
        ],
        'target': 's3',
        'verify': [['lsf', '{target}', '--max-depth', '1']],
        'verify_hint': '请确认 Account ID、Bucket、Endpoint 和 R2 API Token 权限；对象级 Token 请启用 no_check_bucket。',
    },
    's3': {
        'label': 'S3 兼容对象存储',
        'type': 's3',
        'description': 'AWS S3、MinIO、Backblaze B2、Wasabi 等；填写服务商、Endpoint 与 Bucket。',
        'fields': [
            _field('provider', '服务商', type='select', default='AWS', options=S3_PROVIDERS,
                   allow_custom=True, pattern=r'[A-Za-z0-9-]{1,64}',
                   help='选择 rclone 的 provider；不确定时选 Other。'),
            _field('access_key_id', 'Access Key ID', maxlength=128, reuse_blank=True,
                   pattern=r'[A-Za-z0-9]{8,128}', pattern_message='Access Key ID 格式不正确。',
                   help='已有配置可留空保留。'),
            _field('secret_access_key', 'Secret Access Key', type='password', secret=True, reuse_blank=True,
                   help='留空保留已保存的密钥。'),
            _field('region', 'Region', default='us-east-1', maxlength=64, pattern=r'[A-Za-z0-9-]*',
                   pattern_message='Region 只能包含字母、数字和短横线。',
                   help='AWS 例如 us-east-1；其他服务商按文档填写，可留空。'),
            _field('endpoint', 'Endpoint', maxlength=512, placeholder='https://s3.example.com',
                   help='AWS 可留空；MinIO/B2/Wasabi 等必填。'),
            _field('bucket', 'Bucket', config=False, required=True, maxlength=63, pattern=BUCKET_PATTERN,
                   pattern_message='Bucket 名称需为 3～63 位小写字母、数字或短横线，并以字母或数字开头和结尾。',
                   help=BUCKET_HELP),
            _field('path', '桶内保存路径', config=False, default='VaultBackup', maxlength=512,
                   help='可选；留空表示桶根目录。'),
            _field('no_check_bucket', '对象级 Token（no_check_bucket）', type='checkbox', default=False,
                   help='对象级 Token 或无法读取桶信息时启用。'),
        ],
        'target': 's3',
        'verify': [['lsf', '{target}', '--max-depth', '1']],
        'verify_hint': '请确认服务商、Endpoint、Region、Bucket 和凭据。',
    },
    'webdav': {
        'label': 'WebDAV',
        'type': 'webdav',
        'description': '坚果云、Nextcloud 等 WebDAV 服务；保存时会创建并读取远程目录。',
        'fields': [
            _field('url', 'WebDAV 地址', required=True, maxlength=512, placeholder='https://dav.example.com/dav/',
                   pattern=r'https://[^\s]+', pattern_message='请输入 HTTPS WebDAV 地址。',
                   help='填写服务商提供的完整 WebDAV 地址。'),
            _field('vendor', '服务商类型', type='select', default='other', options=WEBDAV_VENDORS,
                   allow_custom=True, pattern=r'[A-Za-z0-9-]{1,64}',
                   help='按服务商选择；不确定时选通用 WebDAV。'),
            _field('user', '用户名', required=True, maxlength=256, help='通常是邮箱或独立的 WebDAV 账号。'),
            _field('pass', '应用密码 / 授权码', type='password', secret=True, reuse_blank=True,
                   help='留空保留已保存的密码。'),
            _field('directory', '保存目录', config=False, default='/VaultBackup', maxlength=512,
                   help='远程目录；保存时会在远端创建。'),
        ],
        'target': 'dir',
        'verify': [['mkdir', '{target}'], ['lsd', '{target}']],
        'verify_hint': '请确认地址、账号、应用密码和目录权限。',
    },
    'sftp': {
        'label': 'SFTP',
        'type': 'sftp',
        'description': '密码认证的 SFTP；密钥认证请使用下方 Rclone 向导，并确保密钥在备份容器内可读。',
        'fields': [
            _field('host', '服务器地址', required=True, maxlength=256, placeholder='sftp.example.com'),
            _field('port', '端口', type='number', default=22, min=1, max=65535),
            _field('user', '用户名', required=True, maxlength=256),
            _field('pass', '密码', type='password', secret=True, reuse_blank=True,
                   help='留空保留已保存的密码。'),
            _field('directory', '保存目录', config=False, default='/VaultBackup', maxlength=512),
        ],
        'target': 'dir',
        'verify': [['mkdir', '{target}'], ['lsd', '{target}']],
        'verify_hint': '请确认地址、端口、账号、密码和目录权限。',
    },
    'ftp': {
        'label': 'FTP',
        'type': 'ftp',
        'description': 'FTP / FTPS；保存时会创建并读取远程目录。',
        'fields': [
            _field('host', '服务器地址', required=True, maxlength=256, placeholder='ftp.example.com'),
            _field('port', '端口', type='number', default=21, min=1, max=65535),
            _field('user', '用户名', required=True, maxlength=256),
            _field('pass', '密码', type='password', secret=True, reuse_blank=True),
            _field('tls', '使用隐式 TLS（FTPS）', type='checkbox', default=False,
                   help='显式 FTPS 请使用 Rclone 向导。'),
            _field('directory', '保存目录', config=False, default='/VaultBackup', maxlength=512),
        ],
        'target': 'dir',
        'verify': [['mkdir', '{target}'], ['lsd', '{target}']],
        'verify_hint': '请确认地址、端口、账号、密码和目录权限。',
    },
    'local': {
        'label': '本机目录',
        'type': 'local',
        'description': '使用备份容器可访问的本机路径；保存时不做远程验证。',
        'fields': [
            _field('directory', '保存目录', config=False, default='/bitwarden/backup/archive', maxlength=512,
                   help='容器内绝对路径；默认挂载的备份目录。'),
        ],
        'target': 'dir',
        'verify': [],
        'verify_hint': '',
    },
}
ORDER = ['r2', 's3', 'webdav', 'sftp', 'ftp', 'local']


def public_presets():
    result = []
    for preset_id in ORDER:
        preset = PRESETS[preset_id]
        result.append({
            'id': preset_id,
            'label': preset['label'],
            'type': preset['type'],
            'description': preset.get('description', ''),
            'fields': [dict(field) for field in preset['fields']],
        })
    return result


def normalize(preset_id, data):
    preset = PRESETS[preset_id]
    values = {}
    for field in preset['fields']:
        key, kind = field['key'], field.get('type', 'text')
        raw = data.get(key, field.get('default', ''))
        if kind == 'checkbox':
            if not isinstance(raw, bool):
                raise ValueError(field['label'] + '选项格式不正确。')
            values[key] = raw
        elif kind == 'number':
            try:
                number = int(raw)
            except (TypeError, ValueError):
                raise ValueError(field['label'] + '必须是整数。')
            if not field.get('min', -10**9) <= number <= field.get('max', 10**9):
                raise ValueError(field['label'] + '超出范围。')
            values[key] = str(number)
        elif kind == 'select':
            if not isinstance(raw, str):
                raise ValueError(field['label'] + '选择不正确。')
            raw = raw.strip()
            allowed = {option['value'] for option in field.get('options', [])}
            if raw not in allowed and not (field.get('allow_custom') and raw and re.fullmatch(field.get('pattern', r'[A-Za-z0-9-]{1,64}'), raw)):
                raise ValueError(field['label'] + '选择不正确。')
            values[key] = raw
        else:
            if not isinstance(raw, str):
                raise ValueError(field['label'] + '格式不正确。')
            if not field.get('secret'):
                raw = raw.strip()
            if any(ord(c) < 32 for c in raw):
                raise ValueError(field['label'] + '格式不正确。')
            if len(raw) > field.get('maxlength', 512):
                raise ValueError(field['label'] + '过长。')
            if raw and field.get('pattern') and not re.fullmatch(field['pattern'], raw):
                raise ValueError(field.get('pattern_message', field['label'] + '格式不正确。'))
            if field.get('required') and not raw.strip():
                raise ValueError(field['label'] + '不能为空。')
            values[key] = raw
    if preset_id in ('r2', 's3'):
        values['bucket'] = values['bucket'].lower()
        if preset_id == 'r2':
            values['account_id'] = values['account_id'].lower()
            if values['account_id'] and not re.fullmatch(r'[a-f0-9]{32}', values['account_id']):
                raise ValueError('Account ID 应为 32 位十六进制字符。')
            if values.get('jurisdiction') and not re.fullmatch(r'[a-z0-9-]{1,32}', values['jurisdiction']):
                raise ValueError('数据区域格式不正确。')
        if values.get('endpoint'):
            parsed = urlsplit(values['endpoint'])
            try:
                parsed.port
            except ValueError:
                raise ValueError('Endpoint 端口不正确。')
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('Endpoint 不能包含账号、令牌、查询参数或片段。')
        path = values.get('path', '').strip().strip('/')
        parts = Path(path).parts
        if any(part in ('.', '..') or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', part) for part in parts):
            raise ValueError('桶内保存路径只能包含字母、数字、点、下划线和短横线。')
        values['path'] = '/'.join(parts)
        if len(target_dir(preset_id, values)) > 512:
            raise ValueError('Bucket 与桶内保存路径合计不能超过 512 个字符。')
        if preset_id == 's3' and values['provider'] != 'AWS' and not values.get('endpoint'):
            raise ValueError('该服务商必须填写 Endpoint。')
    else:
        if preset_id == 'webdav':
            parsed = urlsplit(values.get('url', ''))
            try:
                parsed.port
            except ValueError:
                raise ValueError('WebDAV 地址端口不正确。')
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('WebDAV 地址不能包含账号、令牌、查询参数或片段，请把账号和密码填到下面的字段。')
        directory = values.get('directory', '/').strip()
        absolute = directory.startswith('/')
        parts = Path(directory).parts
        if '..' in parts or any(ord(c) < 32 for c in directory):
            raise ValueError('保存目录不正确。')
        if len(directory) > 512:
            raise ValueError('保存目录过长。')
        if preset_id == 'local' and not absolute:
            raise ValueError('本机目录请使用绝对路径。')
        normalized = '/'.join(part for part in parts if part not in ('/', '//'))
        values['directory'] = (('/' if absolute else '') + normalized).rstrip('/') or ('/' if absolute else '')
    return values


def target_dir(preset_id, values):
    if preset_id in ('r2', 's3'):
        path = values.get('path', '')
        return '/' + values['bucket'] + ('/' + path if path else '')
    return values.get('directory', '') or '/'


def verify_target(preset_id, name, values):
    if preset_id in ('r2', 's3'):
        path = values.get('path', '')
        return name + ':' + values['bucket'] + ('/' + path if path else '')
    return name + ':' + target_dir(preset_id, values)


def verify_commands(preset_id, remote_target):
    return [[argument.format(target=remote_target) for argument in command]
            for command in PRESETS[preset_id].get('verify', [])]


def config_lines(preset_id, values, obscured):
    preset = PRESETS[preset_id]
    lines = ['type = ' + preset['type']]
    managed = {'type'}
    if preset_id == 'r2':
        suffix = '.' + values['jurisdiction'] if values.get('jurisdiction') else ''
        lines += ['provider = Cloudflare', 'env_auth = false', 'region = auto',
                  'endpoint = https://%s%s.r2.cloudflarestorage.com' % (values['account_id'], suffix), 'acl = private']
        managed |= {'provider', 'env_auth', 'region', 'endpoint', 'acl'}
    elif preset_id == 's3':
        lines += ['env_auth = false']
        managed.add('env_auth')
    for field in preset['fields']:
        if field.get('config') is False:
            continue
        key, option = field['key'], field.get('option', field['key'])
        managed.add(option)
        value = obscured[key] if field.get('secret') else values.get(key, '')
        if field.get('type') == 'checkbox':
            if value:
                lines.append('%s = true' % option)
        elif value not in ('', None):
            lines.append('%s = %s' % (option, value))
    return lines, managed
