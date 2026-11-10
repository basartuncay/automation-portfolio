<?php

namespace App\Http\Controllers;

use App\Enums\InvoiceStatus;
use App\Models\Invoice;
use App\Models\Vendor;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;

/**
 * Receives the JSON produced by the invoice extractor (directly or via n8n) and upserts it.
 * Re-sending the same invoice updates it; invoices already approved, paid or rejected are never overwritten.
 */
class IngestInvoiceController
{
    public function __invoke(Request $request): JsonResponse
    {
        abort_unless($request->user()->tokenCan('invoices:write'), 403, 'Token lacks invoices:write');

        $data = $request->validate([
            'vendor' => ['required', 'string', 'max:255'],
            'number' => ['required', 'string', 'max:64'],
            'date' => ['nullable', 'date'],
            'due_date' => ['nullable', 'date'],
            'currency' => ['required', 'string', 'size:3'],
            'subtotal' => ['nullable', 'numeric', 'min:0'],
            'tax' => ['nullable', 'numeric', 'min:0'],
            'total' => ['required', 'numeric', 'min:0'],
            'file' => ['nullable', 'string', 'max:255'],
            'problems' => ['array'],
            'problems.*' => ['string', 'max:500'],
            'lines' => ['array', 'max:500'],
            'lines.*.description' => ['required', 'string', 'max:255'],
            'lines.*.qty' => ['required', 'integer', 'min:0'],
            'lines.*.unit_price' => ['required', 'numeric'],
            'lines.*.amount' => ['required', 'numeric'],
        ]);

        return DB::transaction(function () use ($data) {
            $vendor = Vendor::firstOrCreate(['name' => $data['vendor']], ['default_currency' => strtoupper($data['currency'])]);
            $invoice = Invoice::where('vendor_id', $vendor->id)->where('number', $data['number'])->lockForUpdate()->first();

            if ($invoice && ! in_array($invoice->status, [InvoiceStatus::Pending, InvoiceStatus::NeedsReview], true)) {
                return response()->json(['id' => $invoice->id, 'status' => $invoice->status, 'updated' => false,
                    'message' => 'Invoice already '.$invoice->status->value.'; not overwritten'], 409);
            }

            $problems = array_values($data['problems'] ?? []);
            $attributes = [
                'invoice_date' => $data['date'] ?? null,
                'due_date' => $data['due_date'] ?? null,
                'currency' => strtoupper($data['currency']),
                'subtotal' => $data['subtotal'] ?? null,
                'tax' => $data['tax'] ?? null,
                'total' => $data['total'],
                'source_file' => $data['file'] ?? null,
                'problems' => $problems ?: null,
                'status' => $problems ? InvoiceStatus::NeedsReview : InvoiceStatus::Pending,
            ];

            $created = ! $invoice;
            $invoice ??= new Invoice(['vendor_id' => $vendor->id, 'number' => $data['number']]);
            $invoice->fill($attributes)->save();
            $invoice->lines()->delete();
            $invoice->lines()->createMany($data['lines'] ?? []);

            return response()->json(['id' => $invoice->id, 'status' => $invoice->status, 'updated' => ! $created], $created ? 201 : 200);
        });
    }
}
