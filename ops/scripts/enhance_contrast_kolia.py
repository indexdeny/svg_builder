"""Legacy CLAHE helper; write an explicit processing copy, never the original."""
import argparse
from pathlib import Path

import cv2

import reference_whitelist as W


def enhance(source, output, root=W.ROOT):
    root = Path(root).resolve()
    source = W.verify_path(source, root)
    output = Path(output).absolute()
    W.reject_links(output, root)
    destination = (root / '02_processing/contrast').resolve()
    if not output.resolve().is_relative_to(destination) or output.suffix.lower() != '.png':
        raise ValueError('Contrast output must be a PNG inside 02_processing/contrast')
    image = cv2.imread(str(source))
    if image is None:
        raise ValueError('Cannot decode approved source')
    lightness, a, b = cv2.split(cv2.cvtColor(image, cv2.COLOR_BGR2LAB))
    enhanced = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(8, 8)).apply(lightness)
    result = cv2.cvtColor(cv2.merge((enhanced, a, b)), cv2.COLOR_LAB2BGR)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output), result):
        raise OSError('Failed to write contrast processing copy')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', help='Exact path to a whitelisted original')
    parser.add_argument('output', type=Path, help='PNG under 02_processing/contrast')
    args = parser.parse_args()
    enhance(args.source, args.output)

if __name__ == "__main__":
    main()
