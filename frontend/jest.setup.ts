import v8 from "node:v8";

import "@testing-library/jest-dom";
// La caja guarda sus ventas en IndexedDB (B7). jsdom no la trae, así que sin
// este polyfill los tests de la cola fallarían por "no existe indexedDB" en vez
// de por lo que están mirando.
import "fake-indexeddb/auto";

// jsdom tampoco expone `structuredClone`, y IndexedDB lo necesita para guardar
// un objeto. Se usa el serializador de V8 y no `JSON.parse(JSON.stringify(...))`
// porque ese perdería Date y Map: un test pasaría con datos que en el navegador
// se guardan distinto.
if (typeof globalThis.structuredClone === "undefined") {
  globalThis.structuredClone = (<T,>(valor: T): T =>
    v8.deserialize(v8.serialize(valor)) as T) as typeof structuredClone;
}

// jsdom no implementa scrollIntoView.
Element.prototype.scrollIntoView = jest.fn();
