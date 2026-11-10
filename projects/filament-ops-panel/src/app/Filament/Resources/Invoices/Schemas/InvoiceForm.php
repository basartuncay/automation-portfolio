<?php

namespace App\Filament\Resources\Invoices\Schemas;

use Filament\Forms\Components\DatePicker;
use Filament\Forms\Components\Select;
use Filament\Forms\Components\TextInput;
use Filament\Forms\Components\Textarea;
use Filament\Schemas\Components\Section;
use Filament\Schemas\Schema;

class InvoiceForm
{
    // Status, approval and payment fields are deliberately absent: they only change through the
    // Approve / Mark paid / Reject actions, which enforce the allowed transitions and record who did it.
    public static function configure(Schema $schema): Schema
    {
        return $schema
            ->components([
                Section::make('Invoice')->columns(2)->schema([
                    Select::make('vendor_id')->relationship('vendor', 'name')->searchable()->preload()->required()
                        ->createOptionForm([TextInput::make('name')->required()->unique('vendors', 'name')]),
                    TextInput::make('number')->required()->maxLength(64),
                    DatePicker::make('invoice_date'),
                    DatePicker::make('due_date')->afterOrEqual('invoice_date'),
                ]),
                Section::make('Amounts')->columns(4)->schema([
                    Select::make('currency')->options(['USD' => 'USD', 'EUR' => 'EUR', 'GBP' => 'GBP', 'TRY' => 'TRY'])
                        ->default('USD')->required(),
                    TextInput::make('subtotal')->numeric()->minValue(0),
                    TextInput::make('tax')->numeric()->minValue(0),
                    TextInput::make('total')->numeric()->minValue(0)->required(),
                ]),
                Textarea::make('notes')->columnSpanFull(),
            ]);
    }
}
