import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";

import { PurchaseGroupsPreview } from "../PurchaseGroupsPreview";
import type {
  PurchaseGroupItem,
  SheetPurchaseGroups,
} from "@/services/ingestion.service";

/**
 * F-H6.d — la vista previa muestra lo que devolvió el SERVIDOR.
 *
 * Es la única oportunidad de revisar el reparto: una vez importado, el envío ya
 * está adentro del costo unitario del producto y no queda a la vista de dónde
 * salió. Por eso los tests miran los importes exactos de la respuesta — si el
 * componente los recalculara, la pantalla podría mostrar una división distinta
 * de la que se persiste.
 */
function grupo(over: Partial<PurchaseGroupItem> = {}): PurchaseGroupItem {
  return {
    comprobante: "A-0001",
    proveedor: "Distribuidora Sur",
    subtotal: "10000.00",
    envio_compartido: "500.00",
    repartido: "500.00",
    sin_repartir: "0.00",
    distribuible: true,
    motivo_no_distribuible: null,
    lineas: [
      {
        row_index: 0,
        producto: "Yerba",
        subtotal: "5000.00",
        envio_asignado: "250.00",
        costo_total: "5250.00",
        costo_unitario_final: "525.00",
      },
      {
        row_index: 1,
        producto: "Azúcar",
        subtotal: "3000.00",
        envio_asignado: "150.00",
        costo_total: "3150.00",
        costo_unitario_final: "315.00",
      },
      {
        row_index: 2,
        producto: "Fideos",
        subtotal: "2000.00",
        envio_asignado: "100.00",
        costo_total: "2100.00",
        costo_unitario_final: null,
      },
    ],
    ...over,
  };
}

function hoja(over: Partial<SheetPurchaseGroups> = {}): SheetPurchaseGroups {
  return {
    context_id: "hoja1",
    label: "Compras",
    puede_distribuir: true,
    motivo: null,
    grupos_total: 1,
    grupos: [grupo()],
    filas_sin_comprobante: 0,
    ...over,
  };
}

describe("PurchaseGroupsPreview", () => {
  test("muestra el comprobante con sus líneas y el reparto del servidor", () => {
    render(<PurchaseGroupsPreview hoja={hoja()} />);
    expect(screen.getByText(/A-0001/)).toBeInTheDocument();
    expect(screen.getByText(/Distribuidora Sur/)).toBeInTheDocument();
    expect(screen.getByText(/3 líneas/)).toBeInTheDocument();
    expect(screen.getByText(/\$10\.000 de mercadería/)).toBeInTheDocument();
    expect(
      screen.getByText(/\$500 de envío se reparten \$250 \/ \$150 \/ \$100/),
    ).toBeInTheDocument();
  });

  test("cada línea muestra el costo que va a quedar guardado", () => {
    // El porcentaje del reparto es abstracto; el número que importa es el costo
    // con el que se va a valuar ese producto.
    render(<PurchaseGroupsPreview hoja={hoja()} />);
    expect(screen.getByText(/\$5\.250/)).toBeInTheDocument();
    expect(screen.getByText(/\$525 por unidad/)).toBeInTheDocument();
    // Sin cantidad no hay costo unitario: no se inventa uno.
    expect(screen.queryByText(/\$2\.100 \(.*por unidad/)).not.toBeInTheDocument();
  });

  test("una respuesta acotada dice que está acotada", () => {
    // Mostrar 20 sin aclarar que hay 143 se lee como «esto es todo el archivo».
    render(
      <PurchaseGroupsPreview
        hoja={hoja({ grupos_total: 143, grupos: [grupo(), grupo({ comprobante: "A-0002" })] })}
      />,
    );
    expect(screen.getByText(/Mostrando 2 de 143 comprobantes/)).toBeInTheDocument();
  });

  test("no dice «mostrando» cuando la lista está completa", () => {
    render(<PurchaseGroupsPreview hoja={hoja()} />);
    expect(screen.queryByText(/Mostrando/)).not.toBeInTheDocument();
    expect(screen.getByText(/1 comprobante\./)).toBeInTheDocument();
  });

  test("traduce el motivo de la hoja a castellano, no muestra el código", () => {
    render(
      <PurchaseGroupsPreview
        hoja={hoja({
          puede_distribuir: false,
          motivo: "sin_identidad_de_comprobante",
          grupos: [],
          grupos_total: 0,
        })}
      />,
    );
    expect(screen.getByText(/no traen número de comprobante/i)).toBeInTheDocument();
    expect(screen.queryByText(/sin_identidad_de_comprobante/)).not.toBeInTheDocument();
  });

  test("traduce también el motivo de un comprobante suelto", () => {
    render(
      <PurchaseGroupsPreview
        hoja={hoja({
          grupos: [
            grupo({
              distribuible: false,
              motivo_no_distribuible: "cifras_distintas_de_envio",
            }),
          ],
        })}
      />,
    );
    expect(screen.getByText(/importes de envío distintos/i)).toBeInTheDocument();
    expect(screen.queryByText(/cifras_distintas_de_envio/)).not.toBeInTheDocument();
  });

  test("un motivo desconocido se muestra crudo en vez de tragarse el aviso", () => {
    // Que aparezca un texto feo es mucho menos grave que ocultar que el
    // servidor dijo que algo no se pudo repartir.
    render(
      <PurchaseGroupsPreview
        hoja={hoja({ puede_distribuir: false, motivo: "motivo_nuevo_del_backend" })}
      />,
    );
    expect(screen.getByText(/motivo_nuevo_del_backend/)).toBeInTheDocument();
  });

  test("avisa cuando parte del envío quedó sin repartir", () => {
    // No es un detalle de redondeo: es plata que quedó como gasto en vez de
    // entrar al costo de la mercadería.
    render(
      <PurchaseGroupsPreview hoja={hoja({ grupos: [grupo({ sin_repartir: "12.50" })] })} />,
    );
    expect(screen.getByText(/\$12,5 del envío no se repartieron/)).toBeInTheDocument();
  });

  test("avisa por las filas que no tienen comprobante", () => {
    render(<PurchaseGroupsPreview hoja={hoja({ filas_sin_comprobante: 4 })} />);
    expect(screen.getByText(/4 filas sin número de comprobante/)).toBeInTheDocument();
  });

  test("sin comprobantes, sin motivo y sin huérfanas no renderiza nada", () => {
    const { container } = render(
      <PurchaseGroupsPreview hoja={hoja({ grupos: [], grupos_total: 0 })} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
