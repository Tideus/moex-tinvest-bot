// Run inside the existing Kuma container. Credentials ONLY in environment.
// Default dry-run; --apply required. No monitor deletion or broker access.
const { io } = require("socket.io-client");
const targets = [
    ["api:tinvest_sandbox", "T-Invest SANDBOX — account API", "sandbox-invest-public-api.tbank.ru"],
    ["api:tinvest_prod", "T-Invest PROD — IIS account API", "invest-public-api.tbank.ru"],
    ["api:moex_iss", "MOEX ISS — public market data", "iss.moex.com"],
    ["api:algopack_tradestats", "ALGOPACK — TradeStats", "apim.moex.com"],
    ["api:algopack_orderstats", "ALGOPACK — OrderStats", "apim.moex.com"],
    ["api:algopack_obstats", "ALGOPACK — OBStats", "apim.moex.com"],
    ["api:calendar", "T-Invest PROD — investor calendar (read-only)", "invest-public-api.tbank.ru"],
    ["api:telegram_chat", "Telegram — trading reports chat", "api.telegram.org"],
    ["api:telegram_alert_chat", "Telegram — infrastructure alerts chat", "api.telegram.org"],
];
const legacy = {
    "Syncthing GUI": ["Infrastructure / Syncthing GUI", "Infrastructure"],
    "MOEX / Syncthing GUI": ["Infrastructure / Syncthing GUI", "Infrastructure"],
    "MOEX / MinIO S3": ["Infrastructure / MinIO S3", "Infrastructure"],
    "MOEX / Observer heartbeat": ["MOEX Trading / Observer heartbeat", "MOEX Trading"],
    "MOEX / Resources": ["Server / VM disk, inodes and available RAM", "Server"],
    "MOEX / Timers and services": ["MOEX Trading / Trading timers and services", "MOEX Trading"],
    "MOEX / API integrations": ["MOEX Trading / All API sources (aggregate)", "MOEX Trading"],
    "MOEX / Trading data": ["MOEX Trading / Data freshness, calendar and report queue", "MOEX Trading"],
};
const apply = process.argv.includes("--apply");
const socket = io("http://127.0.0.1:3001", { transports: ["websocket"], reconnection: false });
const timer = setTimeout(() => finish(2, "FAIL: provisioning timed out"), 60000);
function finish(code, text) {
    console.log(text); clearTimeout(timer); socket.close(); process.exit(code);
}
function rpc(event, ...args) {
    return new Promise((resolve, reject) => socket.timeout(10000).emit(event, ...args,
        (error, response) => error || !response?.ok ? reject(new Error(event)) : resolve(response)));
}
socket.on("connect_error", () => finish(2, "FAIL: local Kuma connection"));
socket.on("connect", () => setTimeout(async () => {
    try {
        if (!process.env.KUMA_USERNAME || !process.env.KUMA_PASSWORD) throw new Error("credentials");
        await rpc("login", { username: process.env.KUMA_USERNAME,
            password: process.env.KUMA_PASSWORD, token: "" });
        const response = await rpc("getMonitorList");
        const monitors = Object.values(response.monitorList || {});
        const plannedNames = monitors.map(m => legacy[m.name]?.[0] || m.name);
        if (new Set(plannedNames).size !== plannedNames.length) {
            throw new Error("planned monitor names collide");
        }
        const groups = {};
        for (const name of ["MOEX Trading", "Server", "Infrastructure"]) {
            const existing = monitors.filter(m => m.name === name);
            if (existing.length > 1 || existing[0] && existing[0].type !== "group") {
                throw new Error("ambiguous group");
            }
            if (existing.length) groups[name] = existing[0].id;
            else {
                console.log(`${apply ? "CREATE" : "WOULD CREATE"} group: ${name}`);
                groups[name] = apply ? (await rpc("add", { name, type: "group", interval: 60,
                    maxretries: 0, retryInterval: 60, notificationIDList: {} })).monitorID : -1;
            }
        }
        for (const monitor of monitors) {
            let change = legacy[monitor.name];
            // Idempotence for renamed monitors and infrastructure aliases.
            if (!change) change = Object.values(legacy).find(v => v[0] === monitor.name);
            if (!change) continue;
            const [name, group] = change;
            if (monitor.name === name && monitor.parent === groups[group]) continue;
            console.log(`${apply ? "UPDATE" : "WOULD UPDATE"} monitor: ${name}`);
            if (apply) await rpc("editMonitor", { ...monitor, name, parent: groups[group] });
        }
        const aggregate = monitors.find(m => /API integrations|All API sources/.test(m.name));
        for (const [key, title, host] of targets) {
            const name = `MOEX Trading / ${title} [${key}]`;
            const matches = monitors.filter(m => m.name === name || m.name === `MOEX / ${key}`);
            if (matches.length > 1) throw new Error("duplicate source monitors");
            if (matches.length) {
                if (matches[0].type !== "push") throw new Error("wrong monitor type");
                if (apply && (matches[0].name !== name || matches[0].parent !== groups["MOEX Trading"])) {
                    await rpc("editMonitor", { ...matches[0], name, parent: groups["MOEX Trading"] });
                }
                continue;
            }
            console.log(`${apply ? "CREATE" : "WOULD CREATE"} source: ${title}`);
            if (apply) await rpc("add", { name, type: "push", interval: 600, maxretries: 1,
                retryInterval: 60, upsideDown: false, parent: groups["MOEX Trading"],
                description: `${host}. Read-only availability probe; no orders. Code: ${key}. ` +
                    "Inspect sudo moex-botctl diagnose; do not retry broker orders.",
                notificationIDList: aggregate?.notificationIDList || {} });
        }
        finish(0, apply ? "PASS: monitor provisioning completed; no secrets printed" :
            "PASS: dry-run; no configuration modified");
    } catch (_) { finish(2, "FAIL: provisioning; inspect authentication/schema, secrets omitted"); }
}, 500));
