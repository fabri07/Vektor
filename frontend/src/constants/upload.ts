// Límite único de subida de archivos, alineado con el backend
// (`MAX_FILE_SIZE_BYTES` en file_parsing.py = 16 MB para todos los endpoints).
// Usar SIEMPRE estas constantes en los controles de upload del frontend para que
// el mensaje al usuario coincida con lo que el servidor realmente acepta.
export const MAX_UPLOAD_MB = 16;
export const MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024;
export const UPLOAD_SIZE_HINT = `El archivo debe ser menor a ${MAX_UPLOAD_MB} MB`;
