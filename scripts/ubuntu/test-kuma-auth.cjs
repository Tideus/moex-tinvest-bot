// Run inside the existing Kuma container, without credentials or state changes.
const { io } = require("socket.io-client");
const socket = io("http://127.0.0.1:3001", {
    transports: ["websocket"],
    reconnection: false,
});
const timer = setTimeout(() => {
    console.error("FAIL: authentication test timed out");
    socket.close();
    process.exit(2);
}, 5000);
socket.on("connect", () => {
    // Connection callback can precede installation of async server handlers.
    setTimeout(() => socket.timeout(2000).emit("getMonitorList", (error, response) => {
        clearTimeout(timer);
        const rejected = !error && response?.ok === false;
        console.log(JSON.stringify({ unauthenticatedRejected: rejected }));
        socket.close();
        process.exit(rejected ? 0 : 2);
    }), 500);
});
socket.on("connect_error", () => {
    clearTimeout(timer);
    console.error("FAIL: Socket.IO connection failed");
    socket.close();
    process.exit(2);
});
