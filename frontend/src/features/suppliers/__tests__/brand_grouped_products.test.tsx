import "@testing-library/jest-dom";
import { render, screen, fireEvent } from "@testing-library/react";

import { BrandGroupedProducts } from "../BrandGroupedProducts";
import type { SupplierBrandGroup } from "@/services/suppliers.service";

const groups: SupplierBrandGroup[] = [
  {
    brand: "Coca Cola",
    is_official: true,
    products: [
      {
        product_id: "p1",
        name: "Coca 1.5L",
        last_purchase_at: "2026-05-01T10:00:00",
        total_qty: 3,
        unit_price: 1500,
      },
    ],
  },
  {
    brand: null,
    is_official: false,
    products: [
      {
        product_id: "p2",
        name: "Cajón de fruta",
        last_purchase_at: null,
        total_qty: 2,
        unit_price: 800,
      },
    ],
  },
];

describe("BrandGroupedProducts", () => {
  it("agrupa por marca y etiqueta los productos sin marca como genéricos", () => {
    render(<BrandGroupedProducts groups={groups} supplierName="Distribuidora Sur" />);

    expect(screen.getByText("Coca Cola")).toBeInTheDocument();
    expect(screen.getByText("Productos genéricos")).toBeInTheDocument();
    expect(screen.getByText("Coca 1.5L")).toBeInTheDocument();
    expect(screen.getByText("Cajón de fruta")).toBeInTheDocument();
  });

  it("muestra el badge Oficial solo en el grupo con is_official", () => {
    render(<BrandGroupedProducts groups={groups} supplierName="Distribuidora Sur" />);

    const oficial = screen.getAllByText("Oficial");
    expect(oficial).toHaveLength(1);
  });

  it("no renderiza nada sin grupos", () => {
    const { container } = render(
      <BrandGroupedProducts groups={[]} supplierName="Distribuidora Sur" />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("filtra por nombre de producto (sin tildes) y oculta grupos sin matches", () => {
    render(<BrandGroupedProducts groups={groups} supplierName="Distribuidora Sur" />);

    // "cajon" (sin tilde) debe matchear "Cajón de fruta".
    fireEvent.change(screen.getByLabelText("Buscar producto…"), {
      target: { value: "cajon" },
    });

    expect(screen.getByText("Cajón de fruta")).toBeInTheDocument();
    expect(screen.queryByText("Coca 1.5L")).not.toBeInTheDocument();
    // El grupo sin matches (Coca Cola) se oculta por completo.
    expect(screen.queryByText("Coca Cola")).not.toBeInTheDocument();
  });

  it("muestra mensaje de sin resultados cuando nada matchea", () => {
    render(<BrandGroupedProducts groups={groups} supplierName="Distribuidora Sur" />);

    fireEvent.change(screen.getByLabelText("Buscar producto…"), {
      target: { value: "zzzz" },
    });

    expect(screen.getByText(/Sin resultados/)).toBeInTheDocument();
  });

  it("capea a 25 filas por grupo y expande con 'Mostrar todos'", () => {
    const bigGroup: SupplierBrandGroup[] = [
      {
        brand: "Marca Grande",
        is_official: false,
        products: Array.from({ length: 30 }, (_, i) => ({
          product_id: `bp${i}`,
          name: `Producto ${i}`,
          last_purchase_at: null,
          total_qty: 1,
          unit_price: 100,
        })),
      },
    ];

    render(<BrandGroupedProducts groups={bigGroup} supplierName="Mayorista" />);

    // Solo las primeras 25 filas visibles.
    expect(screen.getByText("Producto 0")).toBeInTheDocument();
    expect(screen.getByText("Producto 24")).toBeInTheDocument();
    expect(screen.queryByText("Producto 25")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("Mostrar todos (30)"));

    expect(screen.getByText("Producto 25")).toBeInTheDocument();
    expect(screen.getByText("Producto 29")).toBeInTheDocument();
  });
});
