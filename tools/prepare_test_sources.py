"""Download immutable, SHA-256 verified test baselines; never fetch credentials."""
import hashlib
from pathlib import Path
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]
URL='https://raw.githubusercontent.com/slebaw301-collab/Kakakeyukat/'
SOURCES={
 'before.py':('5ee80ff48d374c476c5aa48a919b6c94055ff556','4c5fd1dac0fa5980daea68b70b0b5a28682b6c3e1da4cea61899660222c625f6'),
 'tail_before_fix.py':('bd08dbe79d69240f6b759bdbcecd7f89e3824c66','c448077c7d09970005f60658785700d482b5e087ca018cee42ddea1f1c440d9f'),
 'phase2.py':('770ff2809999d71e5bd7cb787587d043129a628a','3ac576163f85c2ff08b63164eae10bdfd1c1b2e2abaa81a417ac705144669f42'),
 'phase1.py':('4e894aaa56a64c8c7ee14f442773250a2ebb73b5','55c30af8324caab8ec1c0c7ac00071aef82598a9af7f4fd7941f44ff97c8b953'),
 'baseline/bot.py':('5bc5b0f78ba07e3d2578a2f5be71601c231b44ef','8295e9c5e2688a6108bd8d15d03df61c7bda2a93363853021339246dbad3468b'),
}
if __name__=='__main__':
 for name,(commit,digest) in SOURCES.items():
    path=ROOT/name
    if path.exists():
        data=path.read_bytes()
    else:
        with urlopen(URL+commit+'/bot.py',timeout=30) as response:data=response.read()
    assert hashlib.sha256(data).hexdigest()==digest,name
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
 print('Verified immutable baselines; production credentials are never used.')
