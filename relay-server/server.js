const { WebSocketServer } = require('ws');

const PORT = process.env.PORT || 8080;
const wss = new WebSocketServer({ port: PORT });

// Two client roles: "pit" (dashboard) and "car" (Pi)
const clients = new Set();
let currentDriver = null;

function broadcast(data, excludeSocket) {
  const msg = JSON.stringify(data);
  for (const client of clients) {
    if (client !== excludeSocket && client.readyState === 1) {
      client.send(msg);
    }
  }
}

function sendTo(role, data) {
  const msg = JSON.stringify(data);
  for (const client of clients) {
    if (client.role === role && client.readyState === 1) {
      client.send(msg);
    }
  }
}

wss.on('connection', (ws, req) => {
  ws.role = null;
  ws.isAlive = true;
  clients.add(ws);

  console.log(`[+] client connected (${clients.size} total)`);

  ws.on('pong', () => { ws.isAlive = true; });

  ws.on('message', (raw) => {
    let msg;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }

    // First message must be a register handshake: { type: "register", role: "pit"|"car" }
    if (msg.type === 'register') {
      ws.role = msg.role;
      console.log(`[~] ${ws.role} registered`);
      // Tell pit the current car connection state
      if (ws.role === 'pit') {
        const carOnline = [...clients].some(c => c.role === 'car');
        ws.send(JSON.stringify({ type: 'car_status', online: carOnline }));
        if (currentDriver) {
          ws.send(JSON.stringify({ type: 'set_driver', name: currentDriver }));
        }
      }
      // Tell pit crew a car just connected
      if (ws.role === 'car') {
        sendTo('pit', { type: 'car_status', online: true });
      }
      return;
    }

    // Pit → car: pit_message  { type: "pit_message", code: "BOX_NOW", text: "...", sub: "..." }
    if (msg.type === 'pit_message' && ws.role === 'pit') {
      console.log(`[→car] ${msg.code}`);
      sendTo('car', msg);
      // Echo to other pit tabs too
      for (const client of clients) {
        if (client !== ws && client.role === 'pit' && client.readyState === 1) {
          client.send(JSON.stringify(msg));
        }
      }
      return;
    }

    // Car → pit: button_press  { type: "button_press", code: "BOX_REQUEST", ts: epoch }
    if (msg.type === 'button_press' && ws.role === 'car') {
      console.log(`[→pit] ${msg.code}`);
      sendTo('pit', msg);
      return;
    }

    // Pit → pit: set_driver  { type: "set_driver", name: "John" }
    if (msg.type === 'set_driver' && ws.role === 'pit') {
      currentDriver = msg.name;
      console.log(`[driver] ${currentDriver || 'none'}`);
      for (const client of clients) {
        if (client !== ws && client.role === 'pit' && client.readyState === 1) {
          client.send(JSON.stringify(msg));
        }
      }
      return;
    }
  });

  ws.on('close', () => {
    const role = ws.role;
    clients.delete(ws);
    console.log(`[-] ${role || 'unknown'} disconnected (${clients.size} total)`);
    if (role === 'car') {
      sendTo('pit', { type: 'car_status', online: false });
    }
  });

  ws.on('error', (err) => {
    console.error(`[!] ws error (${ws.role}):`, err.message);
  });
});

// Heartbeat — kill dead connections every 30s
const heartbeat = setInterval(() => {
  for (const ws of wss.clients) {
    if (!ws.isAlive) {
      ws.terminate();
      continue;
    }
    ws.isAlive = false;
    ws.ping();
  }
}, 30000);

wss.on('close', () => clearInterval(heartbeat));

console.log(`Littlefoot relay server running on port ${PORT}`);
