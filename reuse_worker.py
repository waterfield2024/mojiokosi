"""Private resident GPU worker. Audio exists only in memory, never in result files."""
import base64
import json
import sys
import traceback


def reply(**data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


def main():
    from reuse_runtime import Enhancer
    try:
        enhancer = Enhancer(sys.argv[1])
        # Exercise all lazy CUDA/conv initialization before recording begins.
        enhancer.enhance(b'\x01\x00' * 32000)
        reply(kind='ready')
        for line in sys.stdin:
            request = json.loads(line)
            if request.get('command') != 'enhance':
                raise ValueError('ノイズ除去の要求が正しくありません。')
            pcm = base64.b64decode(request['pcm'], validate=True)
            output = enhancer.enhance(pcm)
            reply(kind='result', pcm=base64.b64encode(output).decode('ascii'))
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        reply(kind='error', message=str(exc))


if __name__ == '__main__':
    main()
