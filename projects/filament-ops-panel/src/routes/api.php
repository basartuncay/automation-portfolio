<?php

use App\Http\Controllers\IngestInvoiceController;
use Illuminate\Support\Facades\Route;

Route::middleware(['auth:sanctum', 'throttle:120,1'])->group(function () {
    Route::post('/invoices', IngestInvoiceController::class);
});
