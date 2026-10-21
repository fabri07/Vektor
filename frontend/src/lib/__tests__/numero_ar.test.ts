import { parseNumeroAr } from "../numeroAr";

test.each([
  ["1.000", 1000],
  ["12.345.678", 12345678],
  ["1.500,50", 1500.5],
  ["1500,5", 1500.5],
  ["1500", 1500],
  ["12.5", 12.5],
  ["$ 2.000", 2000],
  [" 3 ", 3],
])("«%s» → %s", (texto, esperado) => {
  expect(parseNumeroAr(texto)).toBe(esperado);
});

test.each(["", "abc", "1,2,3", "1.00,5", "-5"])("«%s» no se entiende", (texto) => {
  expect(parseNumeroAr(texto)).toBeNull();
});
