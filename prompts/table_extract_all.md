# Table Extraction

You are given a region of lines extracted from an excel sheet cell grid, followed by a description of how that region is laid out.

For each table described, report: its bounding box (start row, end row, start column, end column) covering the table together with its title, heading and note rows; the rows that name its columns; the first and last row holding a data record; any rows between those two that are not data records; and the row that carries the table's title, or null if no row does.
