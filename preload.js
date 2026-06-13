const { contextBridge, ipcRenderer } = require('electron');

function makeHandler(callback) {
  return (_e, data) => callback(data);
}

contextBridge.exposeInMainWorld('peonBridge', {
  onEvent: (callback) => {
    const handler = makeHandler(callback);
    ipcRenderer.on('peon-event', handler);
    return () => ipcRenderer.removeListener('peon-event', handler);
  },
  onSessionUpdate: (callback) => {
    const handler = makeHandler(callback);
    ipcRenderer.on('session-update', handler);
    return () => ipcRenderer.removeListener('session-update', handler);
  },
  startDrag: () => ipcRenderer.send('drag-start'),
  stopDrag: () => ipcRenderer.send('drag-stop'),
  onConfig: (callback) => {
    const handler = makeHandler(callback);
    ipcRenderer.on('peon-config', handler);
    return () => ipcRenderer.removeListener('peon-config', handler);
  },
});
