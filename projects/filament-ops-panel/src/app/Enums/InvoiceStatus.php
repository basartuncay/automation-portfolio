<?php

namespace App\Enums;

use Filament\Support\Contracts\HasColor;
use Filament\Support\Contracts\HasLabel;

enum InvoiceStatus: string implements HasColor, HasLabel
{
    case Pending = 'pending';
    case NeedsReview = 'needs_review';
    case Approved = 'approved';
    case Paid = 'paid';
    case Rejected = 'rejected';

    public function getLabel(): string
    {
        return match ($this) {
            self::NeedsReview => 'Needs review',
            default => ucfirst($this->value),
        };
    }

    public function getColor(): string
    {
        return match ($this) {
            self::Pending => 'gray',
            self::NeedsReview => 'warning',
            self::Approved => 'info',
            self::Paid => 'success',
            self::Rejected => 'danger',
        };
    }

    /** Statuses an invoice may move to from this one. Paid and rejected are final. */
    public function canBecome(self $next): bool
    {
        return in_array($next, match ($this) {
            self::Pending, self::NeedsReview => [self::Approved, self::Rejected],
            self::Approved => [self::Paid, self::Rejected],
            self::Paid, self::Rejected => [],
        }, true);
    }
}
