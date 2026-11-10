<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

/** Append-only audit trail: one row per change to an invoice. Never updated or deleted by the app. */
class InvoiceEvent extends Model
{
    public const UPDATED_AT = null;

    protected $fillable = ['invoice_id', 'user_id', 'action', 'changes'];

    protected function casts(): array
    {
        return ['changes' => 'array', 'created_at' => 'datetime'];
    }

    public function invoice(): BelongsTo
    {
        return $this->belongsTo(Invoice::class);
    }

    public function user(): BelongsTo
    {
        return $this->belongsTo(User::class);
    }
}
