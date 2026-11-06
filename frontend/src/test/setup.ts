const store: Record<string, string> = {};

const storageMock = {
  getItem: (key: string) => store[key] || null,
  setItem: (key: string, value: string) => {
    store[key] = value.toString();
  },
  removeItem: (key: string) => {
    delete store[key];
  },
  clear: () => {
    for (const k in store) delete store[k];
  },
  length: 0,
  key: (i: number) => Object.keys(store)[i] || null,
};

Object.defineProperty(globalThis, 'localStorage', {
  value: storageMock,
  configurable: true,
  writable: true,
});

if (typeof window !== 'undefined') {
  Object.defineProperty(window, 'localStorage', {
    value: storageMock,
    configurable: true,
    writable: true,
  });
}
