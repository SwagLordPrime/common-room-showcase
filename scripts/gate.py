"""Capture actual RED/GREEN source, test and output pairs without replacing prior evidence."""
import argparse, datetime, hashlib, json, os, pathlib, shutil, subprocess, sys
from zoneinfo import ZoneInfo
ROOT=pathlib.Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("gate")
    parser.add_argument("phase",choices=["red","green"])
    parser.add_argument("module",nargs="+")
    parser.add_argument("--red-pattern",default="NotImplementedError: CR-")
    args=parser.parse_args()
    evidence=ROOT/"evidence"
    stem=args.gate+"-"+args.phase
    receipt=evidence/(stem+".json")
    if receipt.exists(): raise SystemExit("Evidence exists: choose a documented revision, never overwrite")
    snapshot=evidence/(stem+"-snapshot")
    snapshot.mkdir()
    paths=sorted([*ROOT.glob("common_room/*.py"),*ROOT.glob("tests/*.py"),*ROOT.glob("web/*")])
    pairs={}
    for path in paths:
        relative=path.relative_to(ROOT)
        destination=snapshot/relative
        destination.parent.mkdir(parents=True,exist_ok=True)
        data=path.read_bytes()
        destination.write_bytes(data)
        pairs[relative.as_posix()]={"sha256":hashlib.sha256(data).hexdigest(),
            "snapshot":destination.relative_to(ROOT).as_posix()}
    started=datetime.datetime.now(datetime.timezone.utc).isoformat()
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE="1",PYTHONUTF8="1")
    result=subprocess.run([sys.executable,"-m","unittest",*args.module,"-v"],cwd=ROOT,env=env,capture_output=True,timeout=90)
    output=result.stdout+result.stderr
    out=evidence/(stem+".txt");out.write_bytes(output)
    expected=(result.returncode!=0 and args.red_pattern.encode("utf-8") in output) if args.phase=="red" else result.returncode==0
    record={"gate":args.gate,"phase":args.phase,"started_utc":started,
        "finished_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "clock_source":"Python datetime system clock",
        "finished_ct":datetime.datetime.now(ZoneInfo("America/Chicago")).isoformat(),
        "command":[sys.executable,"-m","unittest",*args.module,"-v"],"exit_code":result.returncode,
        "expected_result_observed":expected,"red_pattern":args.red_pattern if args.phase=="red" else None,"source_test_pairs":pairs,
        "output_file":out.relative_to(ROOT).as_posix(),"output_sha256":hashlib.sha256(output).hexdigest(),
        "status":"DEVELOPMENT EVIDENCE; independent gate acceptance separate"}
    receipt.write_text(json.dumps(record,indent=2)+"\n",encoding="utf-8",newline="\n")
    print(json.dumps({"receipt":str(receipt),"expected_result_observed":expected,"exit_code":result.returncode}))
    print(output.decode(errors="replace")[-6000:])
    return 0 if expected else 1
if __name__=="__main__": raise SystemExit(main())
