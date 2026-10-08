// Stub de verificacion del stack express-postgres-sftp.
//
// Es Express DE VERDAD (instalado en la imagen, no en el arranque: un stub que
// dependa de la red no sirve para verificar un stack recien creado). Y usa las
// credenciales de la DB del proyecto: probar el stack tiene que probar que la
// base se alcanza desde la app, no solo que el proceso arranca.
const express = require('express');
const net = require('net');
const os = require('os');

const app = express();
const PORT = process.env.PORT || 3000;
const DB_HOST = process.env.DB_HOST || 'db';
const DB_PORT = Number(process.env.DB_PORT || 5432);

// El estado de la DB se cachea SOLO cuando da ok: la base tarda 20-30s en
// terminar el initdb y cachear el primer fallo dejaba el stub diciendo
// "no conecta" para siempre aunque un segundo despues estuviera lista.
let db = { txt: 'probando...', ok: false, inFlight: null };
function probe() {
  if (db.ok) return Promise.resolve();
  if (db.inFlight) return db.inFlight;
  db.inFlight = new Promise(resolve => {
    const s = net.connect(DB_PORT, DB_HOST);
    let done = false;
    const fin = (txt, ok) => {
      if (done) return;
      done = true;
      db = { txt, ok, inFlight: null };
      resolve();
    };
    s.setTimeout(4000);
    s.once('connect', () => { s.destroy(); fin(`host ${DB_HOST}:${DB_PORT} alcanzable`, true); });
    s.once('timeout', () => { s.destroy(); fin(`timeout contra ${DB_HOST}:${DB_PORT}`, false); });
    s.once('error', e => fin(`no conecta a ${DB_HOST}:${DB_PORT}: ${e.message}`, false));
  });
  return db.inFlight;
}

app.get('/healthz', (req, res) => res.type('text/plain').send('ok\n'));

app.get('/api/healthz', async (req, res) => {
  for (let i = 0; i < 12 && !db.ok; i++) {
    await probe();
    if (!db.ok) await new Promise(r => setTimeout(r, 2000));
  }
  res.json({
    app: 'express ok',
    express: require('express/package.json').version,
    node: process.version,
    os: `${os.type()} ${os.release()}`,
    db: db.txt,
  });
});

app.use((req, res) => res.status(404).json({ error: 'no existe', path: req.path }));

app.listen(PORT, '0.0.0.0', () => console.log(`[stub] express escuchando en ${PORT}`));
