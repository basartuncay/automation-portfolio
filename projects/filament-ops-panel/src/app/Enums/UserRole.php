<?php

namespace App\Enums;

use Filament\Support\Contracts\HasLabel;

enum UserRole: string implements HasLabel
{
    case Viewer = 'viewer';
    case Approver = 'approver';
    case Payer = 'payer';
    case Admin = 'admin';

    public function getLabel(): string
    {
        return ucfirst($this->value);
    }
}
