#!/bin/sh
# Bundle the parts of three.js that Stage uses into one local ES module (no CDN at runtime).
set -eu
HERE=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"
npm init -y >/dev/null
npm install --no-save --no-audit --no-fund three@0.186.1 esbuild@0.28.2 >/dev/null
cat > entry.js <<'JS'
export {
  WebGLRenderer, WebGLRenderTarget, Scene, OrthographicCamera, Mesh, PlaneGeometry,
  ShaderMaterial, Points, BufferGeometry, BufferAttribute, Float32BufferAttribute, TextureLoader,
  NearestFilter, ClampToEdgeWrapping, SRGBColorSpace, NoColorSpace, Color, Vector2, Vector3, Vector4,
  AdditiveBlending, NormalBlending, Texture, LinearSRGBColorSpace
} from 'three';
JS
# Licence header (copyright + full MIT permission text) at the top of the bundle, as the
# MIT licence requires; esbuild's own legal comments are kept inline as well.
{
  printf '/*! three.js r186 (subset bundled for Stira Stage) - https://threejs.org\n'
  sed 's/\*\//* \//g' node_modules/three/LICENSE
  printf '*/\n'
} > banner.txt
npx esbuild entry.js --bundle --format=esm --minify --legal-comments=inline \
  --banner:js="$(cat banner.txt)" --outfile="$HERE/vendor/three.stage.min.js"
cp node_modules/three/LICENSE "$HERE/vendor/LICENSE.three.txt"
# code only: no three.js example models, textures or addons are copied
