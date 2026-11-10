<?php

namespace App\Filament\Resources\Users\Schemas;

use App\Enums\UserRole;
use Filament\Forms\Components\Select;
use Filament\Forms\Components\TextInput;
use Filament\Schemas\Schema;

class UserForm
{
    public static function configure(Schema $schema): Schema
    {
        return $schema
            ->components([
                TextInput::make('name')->required()->maxLength(255),
                TextInput::make('email')->email()->required()->unique(ignoreRecord: true),
                Select::make('role')->options(UserRole::class)->default(UserRole::Viewer)->required()
                    ->helperText('Approver: approve/reject. Payer: mark paid (never invoices they approved). Admin: everything.'),
                TextInput::make('password')->password()->revealable()
                    ->required(fn (string $operation) => $operation === 'create')
                    ->dehydrated(fn (?string $state) => filled($state)) // blank on edit = keep current password
                    ->minLength(12),
            ]);
    }
}
