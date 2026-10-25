"""macOS system Vision OCR fallback; no downloaded models or Python ML runtime."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

SWIFT = '/usr/bin/swift'
SCRIPT = r'''
import Foundation
import Vision
let url = URL(fileURLWithPath: CommandLine.arguments.last!)
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages = ["zh-Hans", "en-US"]
let handler = VNImageRequestHandler(url: url, options: [:])
try handler.perform([request])
let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
let data = try JSONSerialization.data(withJSONObject: lines)
print(String(data: data, encoding: .utf8)!)
'''


def available():
    return (sys.platform == 'darwin' and Path(SWIFT).is_file()
            and Path('/System/Library/Frameworks/Vision.framework').exists())


def recognize(path):
    if not available(): raise RuntimeError('native_ocr_unavailable')
    with tempfile.TemporaryDirectory(prefix='knowbase-vision-') as td:
        p = subprocess.run([SWIFT,'-module-cache-path',td,'-e',SCRIPT,str(Path(path).resolve())],
                           capture_output=True,text=True,timeout=45)
    if p.returncode: raise RuntimeError('native_ocr_failed')
    values = json.loads(p.stdout)
    if not isinstance(values,list) or not all(isinstance(v,str) for v in values):
        raise RuntimeError('native_ocr_invalid_output')
    return '\n'.join(values)
