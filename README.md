# tenkm.org

Static site: a landing page and an mtv demo on a read-only snapshot of the bruckert-10km loop.

    index.html            landing page
    demo/index.html       mtv's page, with a shim that answers its API from snapshot.json
    demo/snapshot.json    precomputed API responses (built with mtv 0.1.0's data layer)
    demo/vendor/          Apache ECharts (Apache-2.0)

Serve the folder as-is (GitHub Pages, Netlify, S3, any static host). No server code.
To preview: `python3 -m http.server -d . 8000` and open http://127.0.0.1:8000/.
