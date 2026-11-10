<?php

namespace App\Filament\Resources\Invoices\Schemas;

use App\Models\Invoice;
use Filament\Infolists\Components\TextEntry;
use Filament\Schemas\Components\Section;
use Filament\Schemas\Schema;

class InvoiceInfolist
{
    public static function configure(Schema $schema): Schema
    {
        $money = fn (string $field) => TextEntry::make($field)->money(fn (Invoice $record) => $record->currency)->placeholder('—');

        return $schema
            ->components([
                Section::make('Invoice')->columns(3)->schema([
                    TextEntry::make('vendor.name')->label('Vendor'),
                    TextEntry::make('number')->copyable(),
                    TextEntry::make('status')->badge(),
                    TextEntry::make('invoice_date')->date()->placeholder('—'),
                    TextEntry::make('due_date')->date()->placeholder('—')
                        ->color(fn (Invoice $record) => $record->isOverdue() ? 'danger' : null),
                    TextEntry::make('source_file')->placeholder('—'),
                ]),
                Section::make('Amounts')->columns(3)->schema([$money('subtotal'), $money('tax'), $money('total')]),
                Section::make('Review')
                    ->visible(fn (Invoice $record) => filled($record->problems))
                    ->schema([TextEntry::make('problems')->label('Why it needs review')->listWithLineBreaks()->bulleted()->color('warning')]),
                Section::make('Approval')->columns(3)->schema([
                    TextEntry::make('approver.name')->label('Approved by')->placeholder('—'),
                    TextEntry::make('approved_at')->dateTime()->placeholder('—'),
                    TextEntry::make('paid_at')->dateTime()->placeholder('—'),
                    TextEntry::make('notes')->placeholder('—')->columnSpanFull(),
                ]),
            ]);
    }
}
